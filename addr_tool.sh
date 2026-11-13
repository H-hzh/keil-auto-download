#!/usr/bin/env bash
# md32 地址工具启动器（树莓派 / Linux）
#   ./addr_tool.sh                     打开图形界面
#   ./addr_tool.sh --port /dev/ttyUSB0
#   ./addr_tool.sh --port /dev/ttyUSB0 scan
# 可用 PYTHON 环境变量换解释器，例如 PYTHON=python3.11 ./addr_tool.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-python3}"
exec "$PY" "$DIR/addr_tool.py" "$@"
