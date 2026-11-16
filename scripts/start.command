#!/bin/bash
# Finder double-click entry; the repository path may contain spaces or Chinese characters.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$TASK_ROOT"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
TASK_PY="$TASK_ROOT/.venv/bin/python"
if [[ ! -x "$TASK_PY" ]]; then
  echo "未找到项目 .venv。请先按 README.md 的源码运行步骤准备依赖。"
  read -r -p "按回车关闭。" _reply
  exit 1
fi
if ! "$TASK_PY" "$TASK_ROOT/scripts/check-environment.py"; then
  echo "依赖检查失败；上方列出了缺少或无法启动的工具。请按 README.md 处理。"
  read -r -p "按回车关闭。" _reply
  exit 1
fi
export PYTHONPATH="$TASK_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "$TASK_PY" -m app.launcher "$@"
