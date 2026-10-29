@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo 没有找到 Python，请先到 https://www.python.org/downloads/ 安装，并勾选 Add to PATH & pause & exit /b)
if not exist .venv (
  echo 第一次启动，正在安装依赖（大约 2-5 分钟，只需一次）…
  python -m venv .venv || (pause & exit /b)
  call .venv\Scripts\activate.bat
  pip install -q -r requirements.txt || (rmdir /s /q .venv & pause & exit /b)
  python -m playwright install chromium || (rmdir /s /q .venv & pause & exit /b)
) else (
  call .venv\Scripts\activate.bat
)
python -m app.server %*
pause
