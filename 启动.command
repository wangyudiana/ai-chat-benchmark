#!/bin/bash
# 竞品采集台 · Mac 启动脚本（双击运行）
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "没有找到 Python 3。请先到 https://www.python.org/downloads/ 安装，然后重新双击本文件。"
  read -n 1 -s -r -p "按任意键关闭…"; exit 1
fi
if [ ! -d ".venv" ]; then
  echo "第一次启动，正在安装依赖（大约 2-5 分钟，只需一次）…"
  python3 -m venv .venv || { echo "创建虚拟环境失败"; read -n 1 -s -r; exit 1; }
  source .venv/bin/activate
  pip install -q --upgrade pip
  pip install -q -r requirements.txt || { echo "安装依赖失败，请检查网络后重试"; rm -rf .venv; read -n 1 -s -r; exit 1; }
  python -m playwright install chromium || { echo "安装浏览器失败，请检查网络后重试"; rm -rf .venv; read -n 1 -s -r; exit 1; }
else
  source .venv/bin/activate
fi
# 防止采集过程中 Mac 休眠
caffeinate -dimsu -w $$ &
python -m app.server "$@"
