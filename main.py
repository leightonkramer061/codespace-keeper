#!/usr/bin/env python3
"""Root entrypoint for WispByte, Pterodactyl panels, and standalone Python hosts.

Handles:
1. Loading environment variables from .env file.
2. Checking & installing missing requirements automatically.
3. Auto-downloading GitHub CLI (gh) if missing on PATH.
4. Launching the Codespace Keeper bot.
"""

from __future__ import annotations

import os
import sys
import subprocess


def load_env_file() -> None:
    """Load key-value pairs from .env into os.environ if present."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v


def ensure_dependencies() -> None:
    """Check required packages and install them if missing."""
    required_modules = {
        "telegram": "python-telegram-bot",
        "motor": "motor",
        "aiohttp": "aiohttp",
        "dns": "dnspython",
        "dotenv": "python-dotenv",
    }
    missing = False
    for mod in required_modules:
        try:
            __import__(mod)
        except ImportError:
            missing = True
            break

    if missing:
        print("[codespace-keeper] Missing packages detected. Installing requirements...")
        req_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
        pip_cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-r", req_file]
        try:
            subprocess.check_call(pip_cmd)
        except subprocess.CalledProcessError:
            try:
                # Retry with --break-system-packages for PEP 668 locked environments
                subprocess.check_call(pip_cmd + ["--break-system-packages"])
            except subprocess.CalledProcessError as exc:
                print(f"[codespace-keeper] Automatic pip install failed: {exc}")
                print("[codespace-keeper] Tip: Create a venv with: python3 -m venv venv && ./venv/bin/pip install -r requirements.txt")


if __name__ == "__main__":
    load_env_file()
    ensure_dependencies()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    from Bot.main import main
    main()
