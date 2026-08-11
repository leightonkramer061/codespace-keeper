"""Application settings loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    bot_token: str
    mongo_uri: str
    mongo_db: str
    owner_ids: frozenset[int]
    keepalive_interval: int
    watch_interval: int
    schedule_tz: str
    data_dir: str


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise SystemExit("BOT_TOKEN environment variable is required")

    raw_owners = os.getenv("OWNER_IDS", "").replace(",", " ")
    owner_ids = frozenset(int(part) for part in raw_owners.split() if part.strip())

    return Settings(
        bot_token=bot_token,
        mongo_uri=os.getenv("MONGO_URI", "mongodb://mongo:27017"),
        mongo_db=os.getenv("MONGO_DB", "codespace_keeper"),
        owner_ids=owner_ids,
        keepalive_interval=max(30, int(os.getenv("KEEPALIVE_INTERVAL", "300"))),
        watch_interval=max(15, int(os.getenv("WATCH_INTERVAL", "60"))),
        schedule_tz=os.getenv("SCHEDULE_TZ", "Asia/Kolkata").strip() or "Asia/Kolkata",
        data_dir=os.getenv("DATA_DIR", os.path.abspath("./data")),
    )
