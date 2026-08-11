"""Slash-command interface — power-user commands on top of the button UI.

Accounts are addressed by an *alias* (set with /add_account); accounts added
through the button flow can be addressed by their GitHub login.
Codespaces are addressed by their codespace name (as shown by /list).
"""

from __future__ import annotations

import html
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from .gh import GhError
from .handlers import STATE_EMOJI, _ctx, _fmt_hhmm, _fmt_ts, _guard, _parse_hhmm

log = logging.getLogger(__name__)

HELP_TEXT = (
    "\U0001f916 <b>Codespace Keeper — commands</b>\n\n"
    "<b>Accounts</b>\n"
    "/add_account &lt;alias&gt; &lt;PAT&gt; — add GitHub account (message auto-deletes)\n"
    "/remove_account &lt;alias&gt; — remove account + all its codespaces\n"
    "/accounts — list all\n\n"
    "<b>Codespaces</b>\n"
    "/list &lt;alias&gt; — remote + tracked codespaces\n"
    "/add_codespace &lt;alias&gt; &lt;name&gt; — track a codespace\n"
    "/remove_codespace &lt;alias&gt; &lt;name&gt; — untrack\n\n"
    "<b>Startup commands</b>\n"
    "/set_startup &lt;alias&gt; &lt;name&gt; &lt;cmd1;cmd2&gt; — sh command(s), auto-run on every start\n"
    "/set_dir &lt;alias&gt; &lt;name&gt; &lt;dir&gt; — directory to cd into first (e.g. ~/mydirectory)\n"
    "/clear_startup &lt;alias&gt; &lt;name&gt; — clear directory + commands\n\n"
    "<b>Keep-alive</b>\n"
    "/keep &lt;alias&gt; &lt;name&gt; — start (auto-tracks if needed)\n"
    "/stop &lt;alias&gt; &lt;name&gt; — stop keep-alive + shut the codespace down\n"
    "/keep_all — start all tracked\n"
    "/stop_all — stop + shut down everything\n\n"
    "<b>Auto start/stop (daily, IST)</b>\n"
    "/schedule &lt;alias&gt; &lt;name&gt; &lt;stop&gt; &lt;start&gt; — 24h HH:MM times (use - to skip one)\n"
    "/unschedule &lt;alias&gt; &lt;name&gt; — remove the schedule\n\n"
    "<b>Series (rate-limit failover)</b>\n"
    "/series — show the series + status\n"
    "/series_add &lt;alias&gt; &lt;name&gt; — add to the series (order = add order)\n"
    "/series_remove &lt;alias&gt; &lt;name&gt; — remove from the series\n"
    "/series_start — run the series (one codespace at a time)\n"
    "/series_stop — stop the series (shuts the active codespace down)\n"
    "/series_schedule &lt;stop&gt; &lt;start&gt; — daily stop/start for the whole series\n"
    "/series_unschedule — remove the series schedule\n"
    "/series_clear — empty the series\n\n"
    "<b>Info</b>\n"
    "/status — full dashboard\n"
    "/help — this list\n\n"
    "\U0001f4a1 /start opens the button menu — both interfaces share the same data."
)


async def _send(update: Update, text: str) -> None:
    await update.effective_chat.send_message(
        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def _find_account(db, update: Update, alias: str):
    account = await db.get_account_by_alias(update.effective_user.id, alias)
    if not account:
        await _send(
            update,
            f"\u274c No account with alias <code>{html.escape(alias)}</code>. See /accounts.",
        )
    return account


async def _ensure_tracked(update: Update, db, gh, account: dict, name: str):
    """Return the tracked codespace doc, auto-tracking from GitHub if needed."""
    cs = await db.get_codespace_by_name(account["_id"], name)
    if cs:
        return cs
    try:
        remote = await gh.list_codespaces(account)
    except GhError as exc:
        await _send(update, f"\u274c {html.escape(str(exc)[:500])}")
        return None
    for item in remote:
        if item.get("name") == name or item.get("displayName") == name:
            return await db.upsert_codespace(account["_id"], item)
    names = "\n".join(
        f"\u2022 <code>{html.escape(i.get('name', '?'))}</code>" for i in remote
    ) or "(none)"
    await _send(
        update,
        f"\u274c Codespace <code>{html.escape(name)}</code> not found on "
        f"<b>{html.escape(account['login'])}</b>. Available:\n{names}",
    )
    return None


# ----------------------------------------------------------------------
# Accounts
# ----------------------------------------------------------------------

async def cmd_add_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    args = context.args or []
    # Delete the message ASAP — it contains a token.
    try:
        await update.message.delete()
    except Exception:  # noqa: BLE001
        pass
    if len(args) != 2:
        await _send(update, "Usage: <code>/add_account &lt;alias&gt; &lt;PAT&gt;</code>")
        return
    alias, token = args
    try:
        login = await gh.token_login(token)
    except Exception as exc:  # noqa: BLE001
        await _send(update, f"\u274c {html.escape(str(exc))}")
        return
    existing = await db.get_account_by_alias(update.effective_user.id, alias)
    if existing and existing["login"] != login:
        await _send(
            update,
            f"\u274c Alias <code>{html.escape(alias)}</code> already points to "
            f"<b>{html.escape(existing['login'])}</b>. Pick another alias or "
            f"/remove_account it first.",
        )
        return
    await db.add_account(update.effective_user.id, login, token, alias=alias)
    await _send(
        update,
        f"\u2705 Added GitHub account <b>{html.escape(login)}</b> as "
        f"<code>{html.escape(alias)}</code>. (Your token message was deleted.)\n"
        f"Next: <code>/list {html.escape(alias)}</code>",
    )


async def cmd_remove_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    args = context.args or []
    if len(args) != 1:
        await _send(update, "Usage: <code>/remove_account &lt;alias&gt;</code>")
        return
    account = await _find_account(db, update, args[0])
    if not account:
        return
    await keeper.stop_for_account(account["_id"])
    await db.delete_account(account["_id"])
    await _send(
        update,
        f"\U0001f5d1 Removed <b>{html.escape(account['login'])}</b> "
        f"(token, SSH keys and all tracked codespaces deleted).",
    )


async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    accounts = await db.list_accounts(update.effective_user.id)
    if not accounts:
        await _send(
            update,
            "No accounts yet. Add one with "
            "<code>/add_account &lt;alias&gt; &lt;PAT&gt;</code> or via /start.",
        )
        return
    lines = ["\U0001f419 <b>GitHub accounts</b>", ""]
    for a in accounts:
        alias = a.get("alias") or a["login"]
        tracked = await db.list_codespaces(a["_id"])
        running = sum(1 for cs in tracked if keeper.is_running(cs["_id"]))
        key = "\U0001f511" if a.get("ssh_private_key") else ""
        lines.append(
            f"\u2022 <code>{html.escape(alias)}</code> — "
            f"<b>{html.escape(a['login'])}</b> {key} "
            f"({len(tracked)} tracked, {running} \U0001f501)"
        )
    await _send(update, "\n".join(lines))


# ----------------------------------------------------------------------
# Codespaces
# ----------------------------------------------------------------------

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    args = context.args or []
    if len(args) != 1:
        await _send(update, "Usage: <code>/list &lt;alias&gt;</code>")
        return
    account = await _find_account(db, update, args[0])
    if not account:
        return
    tracked = {cs["name"]: cs for cs in await db.list_codespaces(account["_id"])}
    try:
        remote = await gh.list_codespaces(account)
    except GhError as exc:
        await _send(update, f"\u274c {html.escape(str(exc)[:500])}")
        return
    lines = [f"\U0001f5a5 <b>Codespaces — {html.escape(account['login'])}</b>", ""]
    remote_names = set()
    for item in remote:
        name = item.get("name", "?")
        remote_names.add(name)
        cs = tracked.get(name)
        emoji = STATE_EMOJI.get(item.get("state", ""), "\u26aa\ufe0f")
        marks = ""
        if cs:
            marks += " \U0001f4cc"
            if keeper.is_running(cs["_id"]):
                marks += " \U0001f501"
        repo = html.escape(str(item.get("repository") or ""))
        lines.append(f"{emoji}{marks} <code>{html.escape(name)}</code> — {repo}")
    for name, cs in tracked.items():
        if name not in remote_names:
            lines.append(
                f"\u2753 \U0001f4cc <code>{html.escape(name)}</code> — tracked but "
                "not found on GitHub (deleted?)"
            )
    if not remote and not tracked:
        lines.append("(no codespaces)")
    lines.append("")
    lines.append("\U0001f4cc tracked • \U0001f501 keep-alive running")
    await _send(update, "\n".join(lines))


async def cmd_add_codespace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    args = context.args or []
    if len(args) != 2:
        await _send(update, "Usage: <code>/add_codespace &lt;alias&gt; &lt;name&gt;</code>")
        return
    account = await _find_account(db, update, args[0])
    if not account:
        return
    cs = await _ensure_tracked(update, db, gh, account, args[1])
    if cs:
        await _send(
            update,
            f"\U0001f4cc Tracking <code>{html.escape(cs['name'])}</code>. "
            f"Start it with <code>/keep {html.escape(args[0])} {html.escape(cs['name'])}</code>.",
        )


async def cmd_remove_codespace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    args = context.args or []
    if len(args) != 2:
        await _send(update, "Usage: <code>/remove_codespace &lt;alias&gt; &lt;name&gt;</code>")
        return
    account = await _find_account(db, update, args[0])
    if not account:
        return
    cs = await db.get_codespace_by_name(account["_id"], args[1])
    if not cs:
        await _send(update, f"\u274c <code>{html.escape(args[1])}</code> is not tracked.")
        return
    await keeper.stop(cs["_id"])
    await db.delete_codespace(cs["_id"])
    await _send(update, f"\U0001f5d1 Untracked <code>{html.escape(cs['name'])}</code> (keep-alive stopped).")


# ----------------------------------------------------------------------
# Startup commands
# ----------------------------------------------------------------------

async def cmd_set_startup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    args = context.args or []
    if len(args) < 3:
        await _send(
            update,
            "Usage: <code>/set_startup &lt;alias&gt; &lt;name&gt; &lt;cmd1;cmd2&gt;</code>\n"
            "Separate multiple commands with <code>;</code>",
        )
        return
    account = await _find_account(db, update, args[0])
    if not account:
        return
    cs = await _ensure_tracked(update, db, gh, account, args[1])
    if not cs:
        return
    raw = " ".join(args[2:])
    commands = [part.strip() for part in raw.split(";") if part.strip()]
    await db.set_startup_commands(cs["_id"], commands)
    pretty = html.escape("\n".join(commands))
    await _send(
        update,
        f"\u2699\ufe0f Startup commands for <code>{html.escape(cs['name'])}</code> "
        f"({len(commands)}):\n<pre>{pretty}</pre>\n"
        "They run automatically EVERY time the codespace starts \u2014 no matter "
        "when or where you start it (bot, github.com, VS Code, gh CLI). "
        "Background daemons need "
        "<code>nohup … &amp;</code>.",
    )


async def cmd_set_dir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    args = context.args or []
    if len(args) < 3:
        await _send(
            update,
            "Usage: <code>/set_dir &lt;alias&gt; &lt;name&gt; &lt;directory&gt;</code>\n"
            "Example: <code>/set_dir work my-codespace ~/mydirectory</code>\n"
            "Use <code>-</code> to clear (commands run in the home directory).",
        )
        return
    account = await _find_account(db, update, args[0])
    if not account:
        return
    cs = await _ensure_tracked(update, db, gh, account, args[1])
    if not cs:
        return
    raw = " ".join(args[2:]).strip()
    if raw.startswith("cd "):
        raw = raw[3:].strip()
    workdir = None if raw == "-" else raw
    await db.set_startup_dir(cs["_id"], workdir)
    if workdir:
        await _send(
            update,
            f"\U0001f4c2 Startup directory for <code>{html.escape(cs['name'])}</code> "
            f"set to <code>{html.escape(workdir)}</code>.\n"
            f"The sh command(s) from /set_startup now run as "
            f"<code>cd {html.escape(workdir)} &amp;&amp; \u2026</code>",
        )
    else:
        await _send(
            update,
            f"\U0001f4c2 Startup directory cleared for "
            f"<code>{html.escape(cs['name'])}</code> (commands run in home).",
        )


async def cmd_clear_startup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    args = context.args or []
    if len(args) != 2:
        await _send(update, "Usage: <code>/clear_startup &lt;alias&gt; &lt;name&gt;</code>")
        return
    account = await _find_account(db, update, args[0])
    if not account:
        return
    cs = await db.get_codespace_by_name(account["_id"], args[1])
    if not cs:
        await _send(update, f"\u274c <code>{html.escape(args[1])}</code> is not tracked.")
        return
    await db.set_startup_commands(cs["_id"], [])
    await db.set_startup_dir(cs["_id"], None)
    await _send(update, f"\U0001f9f9 Startup directory + commands cleared for <code>{html.escape(cs['name'])}</code>.")


# ----------------------------------------------------------------------
# Keep-alive
# ----------------------------------------------------------------------

async def cmd_keep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    args = context.args or []
    if len(args) != 2:
        await _send(update, "Usage: <code>/keep &lt;alias&gt; &lt;name&gt;</code>")
        return
    account = await _find_account(db, update, args[0])
    if not account:
        return
    cs = await _ensure_tracked(update, db, gh, account, args[1])
    if not cs:
        return
    started = await keeper.start(cs["_id"])
    minutes = settings.keepalive_interval // 60 or 1
    if started:
        await _send(
            update,
            f"\u25b6\ufe0f Keep-alive started for <code>{html.escape(cs['name'])}</code> "
            f"— SSH ping every {minutes} min.",
        )
    else:
        await _send(update, f"\U0001f501 Keep-alive is already running for <code>{html.escape(cs['name'])}</code>.")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    args = context.args or []
    if len(args) != 2:
        await _send(update, "Usage: <code>/stop &lt;alias&gt; &lt;name&gt;</code>")
        return
    account = await _find_account(db, update, args[0])
    if not account:
        return
    cs = await db.get_codespace_by_name(account["_id"], args[1])
    if not cs:
        await _send(update, f"\u274c <code>{html.escape(args[1])}</code> is not tracked.")
        return
    was_running = keeper.is_running(cs["_id"])
    ok = await keeper.stop_and_shutdown(cs["_id"])
    note = "" if was_running else " (no keep-alive was running)"
    if ok:
        await _send(
            update,
            f"\u23f9 Keep-alive stopped and codespace "
            f"<code>{html.escape(cs['name'])}</code> shut down{note}.",
        )
    else:
        await _send(
            update,
            f"\u26a0\ufe0f Keep-alive stopped{note}, but I could not shut "
            f"<code>{html.escape(cs['name'])}</code> down — check /status.",
        )


async def cmd_keep_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    started = already = 0
    for account in await db.list_accounts(update.effective_user.id):
        for cs in await db.list_codespaces(account["_id"]):
            if await keeper.start(cs["_id"]):
                started += 1
            else:
                already += 1
    await _send(
        update,
        f"\u25b6\ufe0f Started {started} keep-alive(s) "
        f"({already} already running). See /status.",
    )


async def cmd_stop_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    stopped = 0
    for account in await db.list_accounts(update.effective_user.id):
        for cs in await db.list_codespaces(account["_id"]):
            if keeper.is_running(cs["_id"]):
                await keeper.stop_and_shutdown(cs["_id"])
                stopped += 1
    await _send(
        update,
        f"\u23f9 Stopped {stopped} keep-alive(s) and shut those codespaces down.",
    )


# ----------------------------------------------------------------------
# Auto start/stop schedule
# ----------------------------------------------------------------------

async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    args = context.args or []
    if len(args) != 4:
        await _send(
            update,
            "Usage: <code>/schedule &lt;alias&gt; &lt;name&gt; &lt;stop&gt; &lt;start&gt;</code>\n"
            f"Times are daily in <b>{html.escape(settings.schedule_tz)}</b>.\n"
            "Example: <code>/schedule work my-codespace 11:30PM 07:00AM</code>\n"
            "Use <code>-</code> to skip one, e.g. <code>/schedule work my-codespace 11:30PM -</code>",
        )
        return
    account = await _find_account(db, update, args[0])
    if not account:
        return
    cs = await _ensure_tracked(update, db, gh, account, args[1])
    if not cs:
        return
    stop_raw, start_raw = args[2], args[3]
    stop_t = None if stop_raw == "-" else _parse_hhmm(stop_raw)
    start_t = None if start_raw == "-" else _parse_hhmm(start_raw)
    if (stop_raw != "-" and stop_t is None) or (start_raw != "-" and start_t is None):
        await _send(
            update,
            "\u274c Invalid time. Send e.g. <code>11:30 PM</code> or <code>23:30</code>.",
        )
        return
    if not stop_t and not start_t:
        await _send(update, "\u274c Set at least one time, or use /unschedule to clear.")
        return
    await db.update_codespace_fields(
        cs["_id"], {"schedule_stop": stop_t, "schedule_start": start_t}
    )
    tz = html.escape(settings.schedule_tz)
    await _send(
        update,
        f"\u23f0 Schedule saved for <code>{html.escape(cs['name'])}</code> ({tz}):\n"
        f"\U0001f6d1 Stop daily at: <b>{_fmt_hhmm(stop_t)}</b>\n"
        f"\u25b6\ufe0f Start daily at: <b>{_fmt_hhmm(start_t)}</b>\n"
        "Keep-alive pauses at stop time and resumes at start time; startup "
        "commands run on every scheduled start.",
    )


async def cmd_unschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    args = context.args or []
    if len(args) != 2:
        await _send(update, "Usage: <code>/unschedule &lt;alias&gt; &lt;name&gt;</code>")
        return
    account = await _find_account(db, update, args[0])
    if not account:
        return
    cs = await db.get_codespace_by_name(account["_id"], args[1])
    if not cs:
        await _send(update, f"\u274c <code>{html.escape(args[1])}</code> is not tracked.")
        return
    await db.update_codespace_fields(
        cs["_id"],
        {"schedule_stop": None, "schedule_start": None, "keepalive_resume": False},
    )
    await _send(update, f"\U0001f9f9 Schedule removed for <code>{html.escape(cs['name'])}</code>.")


# ----------------------------------------------------------------------
# Series: rate-limit failover rotation
# ----------------------------------------------------------------------

async def cmd_series(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    series = await db.get_series()
    cs_ids = [str(x) for x in (series.get("cs_ids") or [])]
    running = bool(series.get("running")) and keeper.series_running()
    active = str(series.get("active")) if series.get("active") else None
    status = "\U0001f7e2 running" if running else "\u26aa\ufe0f stopped"
    lines = [f"\U0001f501 <b>Series</b> — {status}"]
    if not cs_ids:
        lines.append(
            "Empty. Add codespaces with "
            "<code>/series_add &lt;alias&gt; &lt;name&gt;</code> — the order you "
            "add them is the rotation order."
        )
    for i, cid in enumerate(cs_ids, 1):
        cs = await db.get_codespace(cid)
        if not cs:
            continue
        acct = await db.get_account(cs["account_id"])
        alias = (acct.get("alias") or acct.get("login")) if acct else "?"
        marker = " \u25b6\ufe0f active" if (running and cid == active) else ""
        lines.append(
            f"{i}. {html.escape(str(alias))} / "
            f"<code>{html.escape(cs['name'])}</code>{marker}"
        )
    lines.append(
        "\nWhen the active codespace replies with a GitHub rate-limit "
        "error, I stop it and start the next one — looping forever. "
        "Auto start/stop schedules apply to series codespaces too."
    )
    await _send(update, "\n".join(lines))


async def cmd_series_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    args = context.args or []
    if len(args) != 2:
        await _send(update, "Usage: <code>/series_add &lt;alias&gt; &lt;name&gt;</code>")
        return
    account = await _find_account(db, update, args[0])
    if not account:
        return
    cs = await _ensure_tracked(update, db, gh, account, args[1])
    if not cs:
        return
    series = await db.get_series()
    cs_ids = [str(x) for x in (series.get("cs_ids") or [])]
    cid = str(cs["_id"])
    if cid in cs_ids:
        await _send(
            update,
            f"\u2139\ufe0f <code>{html.escape(cs['name'])}</code> is already "
            f"#{cs_ids.index(cid) + 1} in the series.",
        )
        return
    cs_ids.append(cid)
    fields: dict = {"cs_ids": cs_ids}
    if str(series.get("active")) not in cs_ids:
        fields["active"] = cs_ids[0]
    await db.save_series(fields)
    await _send(
        update,
        f"\U0001f501 Added <code>{html.escape(cs['name'])}</code> as "
        f"#{len(cs_ids)} in the series. Start it with /series_start",
    )


async def cmd_series_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    args = context.args or []
    if len(args) != 2:
        await _send(update, "Usage: <code>/series_remove &lt;alias&gt; &lt;name&gt;</code>")
        return
    account = await _find_account(db, update, args[0])
    if not account:
        return
    cs = await db.get_codespace_by_name(account["_id"], args[1])
    if not cs:
        await _send(update, f"\u274c <code>{html.escape(args[1])}</code> is not tracked.")
        return
    series = await db.get_series()
    cs_ids = [str(x) for x in (series.get("cs_ids") or [])]
    cid = str(cs["_id"])
    if cid not in cs_ids:
        await _send(update, f"\u2139\ufe0f <code>{html.escape(cs['name'])}</code> is not in the series.")
        return
    cs_ids.remove(cid)
    fields: dict = {"cs_ids": cs_ids}
    if str(series.get("active")) not in cs_ids:
        fields["active"] = cs_ids[0] if cs_ids else None
    await db.save_series(fields)
    if not cs_ids:
        await keeper.stop_series()
    await _send(update, f"\U0001f9f9 Removed <code>{html.escape(cs['name'])}</code> from the series.")


async def cmd_series_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    ok = await keeper.start_series()
    if ok:
        await _send(
            update,
            "\u25b6\ufe0f Series started. One codespace runs at a time; on a "
            "GitHub rate-limit reply I switch to the next one automatically. "
            "Check /series for the active codespace.",
        )
    else:
        await _send(
            update,
            "\u274c The series is empty. Add codespaces first with "
            "<code>/series_add &lt;alias&gt; &lt;name&gt;</code>",
        )


async def cmd_series_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    await keeper.stop_series()
    await _send(update, "\u23f9 Series stopped (active codespace shut down).")


async def cmd_series_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    await keeper.stop_series()
    await db.save_series(
        {"cs_ids": [], "active": None, "resume": False, "running": False}
    )
    await _send(update, "\U0001f9f9 Series cleared.")


async def cmd_series_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    args = context.args or []
    if len(args) != 2:
        await _send(
            update,
            "Usage: <code>/series_schedule &lt;stop&gt; &lt;start&gt;</code>\n"
            f"Times are daily in <b>{html.escape(settings.schedule_tz)}</b>. "
            "Use <code>-</code> to skip one, e.g. "
            "<code>/series_schedule 11:30PM 07:00AM</code>",
        )
        return
    stop_raw, start_raw = args
    stop_t = None if stop_raw == "-" else _parse_hhmm(stop_raw)
    start_t = None if start_raw == "-" else _parse_hhmm(start_raw)
    if (stop_raw != "-" and stop_t is None) or (start_raw != "-" and start_t is None):
        await _send(
            update,
            "\u274c Invalid time. Send e.g. <code>11:30 PM</code> or <code>23:30</code>.",
        )
        return
    if not stop_t and not start_t:
        await _send(
            update, "\u274c Set at least one time, or use /series_unschedule to clear."
        )
        return
    await db.save_series({"schedule_stop": stop_t, "schedule_start": start_t})
    tz = html.escape(settings.schedule_tz)
    await _send(
        update,
        f"\u23f0 Series schedule saved ({tz}):\n"
        f"\U0001f6d1 Stop daily at: <b>{_fmt_hhmm(stop_t)}</b>\n"
        f"\u25b6\ufe0f Start daily at: <b>{_fmt_hhmm(start_t)}</b>\n"
        "At stop time the series pauses and the active codespace shuts "
        "down; at start time the series resumes from where it left off.",
    )


async def cmd_series_unschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    await db.save_series({"schedule_stop": None, "schedule_start": None})
    await _send(update, "\U0001f9f9 Series schedule removed.")


# ----------------------------------------------------------------------
# Info
# ----------------------------------------------------------------------

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    accounts = await db.list_accounts(update.effective_user.id)
    if not accounts:
        await _send(update, "No accounts yet. Add one with /add_account or /start.")
        return
    lines = ["\U0001f4e1 <b>Dashboard</b>"]
    total_running = 0
    for a in accounts:
        alias = a.get("alias") or a["login"]
        lines.append("")
        lines.append(f"\U0001f419 <b>{html.escape(alias)}</b> ({html.escape(a['login'])})")
        tracked = await db.list_codespaces(a["_id"])
        if not tracked:
            lines.append("   — no tracked codespaces")
            continue
        for cs in tracked:
            running = keeper.is_running(cs["_id"])
            total_running += 1 if running else 0
            emoji = STATE_EMOJI.get(cs.get("state", ""), "\u26aa\ufe0f")
            ka = "\U0001f501 ON" if running else "off"
            last_ping = cs.get("last_ping")
            if last_ping:
                ok = "\u2705" if cs.get("last_ok") else "\u274c"
                when = f"{_fmt_ts(last_ping, settings.schedule_tz, '%I:%M:%S %p')} {ok}"
            else:
                when = "never"
            n_cmds = len(cs.get("startup_commands") or [])
            lines.append(
                f"   {emoji} <code>{html.escape(cs['name'])}</code> — "
                f"keep-alive {ka} • last ping {when} • {n_cmds} startup cmd(s)"
            )
    minutes = settings.keepalive_interval // 60 or 1
    lines.append("")
    lines.append(
        f"\U0001f501 {total_running} keep-alive task(s) running �� ping every {minutes} min"
    )
    await _send(update, "\n".join(lines))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    await _send(update, HELP_TEXT)


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------

def register(app: Application) -> None:
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("accounts", cmd_accounts))
    app.add_handler(CommandHandler("add_account", cmd_add_account))
    app.add_handler(CommandHandler("remove_account", cmd_remove_account))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("add_codespace", cmd_add_codespace))
    app.add_handler(CommandHandler("remove_codespace", cmd_remove_codespace))
    app.add_handler(CommandHandler("set_startup", cmd_set_startup))
    app.add_handler(CommandHandler("set_dir", cmd_set_dir))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("unschedule", cmd_unschedule))
    app.add_handler(CommandHandler("series", cmd_series))
    app.add_handler(CommandHandler("series_add", cmd_series_add))
    app.add_handler(CommandHandler("series_remove", cmd_series_remove))
    app.add_handler(CommandHandler("series_start", cmd_series_start))
    app.add_handler(CommandHandler("series_stop", cmd_series_stop))
    app.add_handler(CommandHandler("series_clear", cmd_series_clear))
    app.add_handler(CommandHandler("series_schedule", cmd_series_schedule))
    app.add_handler(CommandHandler("series_unschedule", cmd_series_unschedule))
    app.add_handler(CommandHandler("clear_startup", cmd_clear_startup))
    app.add_handler(CommandHandler("keep", cmd_keep))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("keep_all", cmd_keep_all))
    app.add_handler(CommandHandler("stop_all", cmd_stop_all))
