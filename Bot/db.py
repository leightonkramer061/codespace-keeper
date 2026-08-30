"""MongoDB persistence layer (accounts + codespaces)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient


def oid(value: Any) -> ObjectId:
    return value if isinstance(value, ObjectId) else ObjectId(str(value))


class Database:
    def __init__(self, uri: str, db_name: str) -> None:
        self.client = AsyncIOMotorClient(uri)
        self.db = self.client[db_name]
        self.accounts = self.db.accounts
        self.codespaces = self.db.codespaces

    async def init(self) -> None:
        await self.accounts.create_index(
            [("tg_user_id", 1), ("login", 1)], unique=True
        )
        await self.codespaces.create_index(
            [("account_id", 1), ("name", 1)], unique=True
        )

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------

    async def add_account(
        self, tg_user_id: int, login: str, token: str, alias: str | None = None
    ) -> dict:
        query = {"tg_user_id": tg_user_id, "login": login}
        set_fields: dict[str, Any] = {"token": token}
        insert_fields: dict[str, Any] = {
            "ssh_private_key": None,
            "ssh_public_key": None,
            "created_at": datetime.now(timezone.utc),
        }
        if alias:
            set_fields["alias"] = alias
        else:
            insert_fields["alias"] = login
        await self.accounts.update_one(
            query,
            {"$set": set_fields, "$setOnInsert": insert_fields},
            upsert=True,
        )
        return await self.accounts.find_one(query)

    async def get_account_by_alias(self, tg_user_id: int, alias: str) -> Optional[dict]:
        """Find an account by its alias, falling back to the GitHub login."""
        return await self.accounts.find_one(
            {"tg_user_id": tg_user_id, "$or": [{"alias": alias}, {"login": alias}]}
        )

    async def list_accounts(self, tg_user_id: int) -> list[dict]:
        cursor = self.accounts.find({"tg_user_id": tg_user_id}).sort("login", 1)
        return await cursor.to_list(length=200)

    async def all_accounts(self) -> list[dict]:
        """Every stored account (used by the codespace start watcher)."""
        return await self.accounts.find({}).to_list(length=1000)

    async def get_account(self, account_id: Any) -> Optional[dict]:
        return await self.accounts.find_one({"_id": oid(account_id)})

    async def delete_account(self, account_id: Any) -> None:
        await self.codespaces.delete_many({"account_id": oid(account_id)})
        await self.accounts.delete_one({"_id": oid(account_id)})

    async def set_ssh_keys(self, account_id: Any, private_key: str, public_key: str) -> None:
        await self.accounts.update_one(
            {"_id": oid(account_id)},
            {"$set": {"ssh_private_key": private_key, "ssh_public_key": public_key}},
        )

    # ------------------------------------------------------------------
    # Codespaces
    # ------------------------------------------------------------------

    async def upsert_codespace(self, account_id: Any, info: dict) -> dict:
        query = {"account_id": oid(account_id), "name": info["name"]}
        await self.codespaces.update_one(
            query,
            {
                "$set": {
                    "display_name": info.get("displayName") or info["name"],
                    "repository": str(info.get("repository") or ""),
                    "state": info.get("state") or "",
                },
                "$setOnInsert": {
                    "startup_commands": [],
                    "startup_done": False,
                    "keepalive": False,
                    "last_ping": None,
                    "last_status": "",
                },
            },
            upsert=True,
        )
        return await self.codespaces.find_one(query)

    async def get_codespace(self, cs_id: Any) -> Optional[dict]:
        return await self.codespaces.find_one({"_id": oid(cs_id)})

    async def get_codespace_by_name(self, account_id: Any, name: str) -> Optional[dict]:
        return await self.codespaces.find_one(
            {
                "account_id": oid(account_id),
                "$or": [{"name": name}, {"display_name": name}],
            }
        )

    async def delete_codespace(self, cs_id: Any) -> None:
        await self.codespaces.delete_one({"_id": oid(cs_id)})

    async def set_startup_dir(self, cs_id: Any, workdir: str | None) -> None:
        """Directory the startup commands cd into before running."""
        await self.codespaces.update_one(
            {"_id": oid(cs_id)}, {"$set": {"startup_dir": workdir}}
        )

    async def update_codespace_fields(self, cs_id: Any, fields: dict) -> None:
        await self.codespaces.update_one({"_id": oid(cs_id)}, {"$set": fields})

    async def all_codespaces(self) -> list[dict]:
        """Every tracked codespace across all accounts (for series selection)."""
        return await self.codespaces.find({}).sort("display_name", 1).to_list(length=1000)

    # ------------------------------------------------------------------
    # Series (rate-limit failover rotation)
    # ------------------------------------------------------------------

    async def get_series(self) -> dict:
        doc = await self.db.series.find_one({"_id": "default"})
        return doc or {
            "_id": "default",
            "cs_ids": [],
            "active": None,
            "running": False,
            "resume": False,
        }

    async def save_series(self, fields: dict) -> None:
        await self.db.series.update_one(
            {"_id": "default"}, {"$set": fields}, upsert=True
        )

    async def list_scheduled(self) -> list[dict]:
        """Codespaces with an auto start/stop schedule set."""
        query = {
            "$or": [
                {"schedule_stop": {"$nin": [None, ""]}},
                {"schedule_start": {"$nin": [None, ""]}},
            ]
        }
        return await self.codespaces.find(query).to_list(length=1000)

    async def list_codespaces(self, account_id: Any) -> list[dict]:
        cursor = self.codespaces.find({"account_id": oid(account_id)}).sort("display_name", 1)
        return await cursor.to_list(length=500)

    async def list_keepalive(self) -> list[dict]:
        cursor = self.codespaces.find({"keepalive": True})
        return await cursor.to_list(length=500)

    async def set_keepalive(self, cs_id: Any, enabled: bool) -> None:
        await self.codespaces.update_one(
            {"_id": oid(cs_id)}, {"$set": {"keepalive": enabled}}
        )

    async def set_state(self, cs_id: Any, state: str) -> None:
        await self.codespaces.update_one({"_id": oid(cs_id)}, {"$set": {"state": state}})

    async def set_startup_commands(self, cs_id: Any, commands: list[str]) -> None:
        await self.codespaces.update_one(
            {"_id": oid(cs_id)},
            {"$set": {"startup_commands": commands, "startup_done": False}},
        )

    async def set_startup_done(self, cs_id: Any, done: bool) -> None:
        await self.codespaces.update_one(
            {"_id": oid(cs_id)}, {"$set": {"startup_done": done}}
        )

    async def record_ping(self, cs_id: Any, ok: bool, detail: str) -> None:
        await self.codespaces.update_one(
            {"_id": oid(cs_id)},
            {
                "$set": {
                    "last_ping": datetime.now(timezone.utc),
                    "last_ok": ok,
                    "last_status": detail,
                }
            },
        )
