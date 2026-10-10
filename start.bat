@echo off
chcp 65001 >nul
cd /d %~dp0
where python >nul 2>nul
if errorlevel 1 (
  echo 未找到 Python。请先安装 Python 3.10 或以上版本,安装时勾选 "Add Python to PATH"。
  echo 下载地址: https://www.python.org/downloads/
  pause
  exit /b 1
)
if not exist .venv (
  echo 首次运行,正在创建虚拟环境并安装依赖,需要几分钟……
  python -m venv .venv
  .venv\Scripts\python -m pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
  if errorlevel 1 (
    echo 依赖安装失败,请检查网络后重试。
    pause
    exit /b 1
  )
)
if not exist .env (
  copy .env.example .env >nul
  echo 已生成 .env 配置文件,请用记事本打开填写大模型密钥后重新运行。现在以演示模式启动。
)
.venv\Scripts\python run.py
pause
