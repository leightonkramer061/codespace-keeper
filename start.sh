#!/usr/bin/env bash
set -e

echo "=================================================="
echo " Starting Codespace Keeper (WispByte Host Runner)"
echo "=================================================="

PYTHON_CMD="python3"
if ! command -v python3 &> /dev/null; then
    PYTHON_CMD="python"
fi

# 1. Create virtual environment if venv doesn't exist
if [ ! -d "venv" ]; then
    echo "[start.sh] Creating Python virtual environment in ./venv..."
    $PYTHON_CMD -m venv venv || python -m venv venv
fi

# 2. Activate virtual environment (POSIX compatible dot syntax)
if [ -f "venv/bin/activate" ]; then
    . venv/bin/activate
fi

# 3. Install requirements using venv pip directly
echo "[start.sh] Installing/verifying dependencies from requirements.txt..."
./venv/bin/pip install --no-cache-dir -r requirements.txt

# 4. Start the bot using venv python directly
echo "[start.sh] Launching bot main.py..."
exec ./venv/bin/python main.py
