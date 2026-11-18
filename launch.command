#!/bin/bash
# Rigol DS1054Z Web Remote — double-click to launch
# Auto-discovers the scope on the network and opens the browser.

cd "$(dirname "$0")"

PORT=8082
SERVER_PID=""

# Cleanup — kill our server process on exit
cleanup() {
    if [ -n "$SERVER_PID" ]; then
        kill "$SERVER_PID" 2>/dev/null
        wait "$SERVER_PID" 2>/dev/null
    fi
}
trap cleanup EXIT INT TERM

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║   Rigol DS1054Z Web Remote GUI       ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

# Find python3.11
PYTHON="/opt/homebrew/bin/python3.11"
if [ ! -x "$PYTHON" ]; then
    PYTHON="$(which python3.11 2>/dev/null)"
fi
if [ ! -x "$PYTHON" ]; then
    PYTHON="$(which python3 2>/dev/null)"
fi
if [ ! -x "$PYTHON" ]; then
    echo "  ERRO: Python 3 não encontrado."
    echo "  Instala com: brew install python@3.11"
    echo ""
    read -p "  Pressiona Enter para fechar..."
    exit 1
fi

echo "  Python: $PYTHON"
echo ""

# Run the server — run.py auto-discovers the scope, handles port conflicts,
# and opens the browser at the correct port automatically
"$PYTHON" run.py --port $PORT "$@"

# If it exits, wait for user
echo ""
echo "  Servidor terminado."
read -p "  Pressiona Enter para fechar..."
