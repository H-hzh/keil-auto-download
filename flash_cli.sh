#!/usr/bin/env bash
# Git Bash / MSYS2 下的启动器（bash 里要 ./flash_cli.sh，或把本目录加进 PATH）
#   ./flash_cli.sh                                  上次的工程 / 内置默认
#   ./flash_cli.sh --build-only
#   ./flash_cli.sh "/d/code/keil/xxx/foo.uvprojx"   换工程（POSIX 路径会自动转成 Windows 路径）
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if command -v cygpath >/dev/null 2>&1; then
    SCRIPT="$(cygpath -w "$DIR/flash_md32.py")"
else
    SCRIPT="$DIR/flash_md32.py"
fi

# 把 /d/xxx 这类 MSYS 路径参数转成 D:\xxx，否则 Windows 版 Python 找不到
# 另外把字面量 app 参数翻译成 --app（应用模式：只编译烧 md32）
ARGS=()
for a in "$@"; do
    case "$a" in
        app) ARGS+=("--app") ;;
        /*)
            if command -v cygpath >/dev/null 2>&1; then
                ARGS+=("$(cygpath -w "$a")")
            else
                ARGS+=("$a")
            fi
            ;;
        *) ARGS+=("$a") ;;
    esac
done

PY=/d/ux-venv/Scripts/python.exe
[ -x "$PY" ] || PY=python

exec "$PY" "$SCRIPT" "${ARGS[@]}"
