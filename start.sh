#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null; then
  echo "未找到 python3,请先安装 Python 3.10+"; exit 1
fi
if [ ! -d .venv ]; then
  echo "首次运行,正在创建虚拟环境并安装依赖……"
  python3 -m venv .venv
  .venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
fi
if [ ! -f .env ]; then
  cp .env.example .env
  echo "已生成 .env,请填写大模型密钥后重启。现在以演示模式启动。"
fi
exec .venv/bin/python run.py
