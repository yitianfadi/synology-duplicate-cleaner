#!/bin/sh
# 便捷包装：在 Linux/macOS 构建机上调用 build_spk.py
set -e
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$DIR/build_spk.py" "$@"
