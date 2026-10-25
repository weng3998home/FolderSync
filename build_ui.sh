#!/bin/bash
set -euo pipefail

# 编译 Qt Designer 的 .ui 文件为 Python 代码
# 用法: bash build_ui.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v pyuic6 >/dev/null 2>&1; then
    echo "编译失败：找不到 pyuic6，请先安装 PyQt6 开发工具。" >&2
    echo "  sudo apt install pyqt6-dev-tools" >&2
    exit 127
fi

echo "正在编译 main_window.ui ..."
if pyuic6 main_window.ui -o ui_main_window.py; then
    echo "编译成功: ui_main_window.py"
    echo
    echo "现在可以运行程序:"
    echo "  python folder_sync.py"
else
    status=$?
    echo "编译失败，请检查 main_window.ui 并查看 pyuic6 错误信息。" >&2
    exit "$status"
fi
