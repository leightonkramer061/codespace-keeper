"""Entrypoint: wires settings, MongoDB, gh wrapper, keeper and Telegram bot."""

from __future__ import annotations

import logging
import shutil

from telegram import BotCommand, Update
from telegram.ext import Application, ApplicationBuilder

from . import commands, handlers
from .config import load_settings
from .db import Database
from .gh import Gh
from .installer import ensure_gh_installed
from .keeper import KeeperManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("codespace-keeper")


async def post_init(app: Application) -> None:
    db: Database = app.bot_data["db"]
    keeper: KeeperManager = app.bot_data["keeper"]
    await db.init()
    await keeper.restore()
    keeper.start_watcher()
    keeper.start_scheduler()
    await keeper.restore_series()
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Open the button menu"),
            BotCommand("help", "Full command list"),
            BotCommand("status", "Full dashboard"),
            BotCommand("accounts", "List GitHub accounts"),
            BotCommand("add_account", "<alias> <PAT> add account"),
            BotCommand("remove_account", "<alias> remove account"),
            BotCommand("list", "<alias> list codespaces"),
            BotCommand("add_codespace", "<alias> <name> track codespace"),
            BotCommand("remove_codespace", "<alias> <name> untrack"),
            BotCommand("set_startup", "<alias> <name> <cmd1;cmd2>"),
            BotCommand("set_dir", "<alias> <name> <directory>"),
            BotCommand("schedule", "<alias> <name> <stop> <start> IST"),
            BotCommand("unschedule", "<alias> <name> remove schedule"),
            BotCommand("series", "Show the rate-limit failover series"),
            BotCommand("series_add", "<alias> <name> add to series"),
            BotCommand("series_remove", "<alias> <name> remove from series"),
            BotCommand("series_start", "Run the series"),
            BotCommand("series_stop", "Stop the series"),
            BotCommand("series_schedule", "<stop> <start> daily series times IST"),
            BotCommand("series_unschedule", "Remove the series schedule"),
            BotCommand("series_clear", "Empty the series"),
            BotCommand("clear_startup", "<alias> <name> clear commands"),
            BotCommand("keep", "<alias> <name> start keep-alive"),
            BotCommand("stop", "<alias> <name> stop keep-alive + codespace"),
            BotCommand("keep_all", "Start all tracked"),
            BotCommand("stop_all", "Stop everything"),
            BotCommand("cancel", "Cancel the current action"),
        ]
    )
    log.info("Bot initialized; keep-alive tasks restored, start watcher running.")


def main() -> None:
    ensure_gh_installed()

    settings = load_settings()
    db = Database(settings.mongo_uri, settings.mongo_db)
    gh = Gh(settings, db)
    keeper = KeeperManager(settings, db, gh)

    app = ApplicationBuilder().token(settings.bot_token).post_init(post_init).build()
    app.bot_data.update(
        {"settings": settings, "db": db, "gh": gh, "keeper": keeper}
    )
    handlers.register(app)
    commands.register(app)

    if not settings.owner_ids:
        log.warning(
            "OWNER_IDS is empty — the bot will accept commands from ANY "
            "Telegram user. Set OWNER_IDS in .env to lock it down."
        )

    log.info("Starting polling (keep-alive interval: %ss)", settings.keepalive_interval)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
