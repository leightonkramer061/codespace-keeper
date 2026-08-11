# Hosting on WispByte (Non-Docker / Locked Python Environment)

This repository has been modified to run smoothly on **WispByte** and other Pterodactyl-based bot hosting platforms without needing Docker or `root` permissions.

## Key Changes Made for WispByte
1. **Auto GitHub CLI (`gh`) Downloader**: No `apt install gh` or root access required. The bot automatically downloads and uses a standalone static `gh` binary in `./bin/gh`.
2. **Locked Python Environment (PEP 668) Support**: Automatically handles virtual environments (`venv`) or pip dependencies without system package errors.
3. **Local Storage Default**: State and SSH keys default to `./data` relative to the bot directory (instead of `/app/data`).
4. **Root Entry Point (`main.py` & `start.sh`)**: Direct launch support via Pterodactyl panel.

---

## Step-by-Step Setup on WispByte

### 1. Upload the Code
Upload all files from this repository to your WispByte server via SFTP or the WispByte File Manager.

### 2. Configure Environment Variables (`.env`)
Create a file named `.env` in the root folder of your bot:

```ini
BOT_TOKEN=123456789:YOUR_TELEGRAM_BOT_TOKEN
OWNER_IDS=YOUR_TELEGRAM_USER_ID
MONGO_URI=mongodb+srv://USERNAME:PASSWORD@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority
MONGO_DB=codespace_keeper
```

*(You can get a free MongoDB cluster at [mongodb.com/atlas](https://www.mongodb.com/atlas)).*

### 3. WispByte Panel Settings
In your WispByte Server Panel:
- **Startup / Main File**: Set to `main.py` (or `start.sh` if running bash).
- **Python Version**: Select Python 3.10+ (Python 3.12 recommended).

### 4. Start the Bot
Click **Start** on WispByte!
- The bot will automatically download the standalone GitHub CLI (`gh`) binary into `./bin/gh`.
- Dependencies from `requirements.txt` will automatically install.
- The bot will connect to Telegram and start polling!
