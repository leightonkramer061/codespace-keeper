"""Telegram handlers: menus, account management, codespace management."""

from __future__ import annotations

import html
import logging
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .gh import GhError

log = logging.getLogger(__name__)

ASK_TOKEN, ASK_CMDS, ASK_DIR, ASK_STOP_TIME, ASK_START_TIME = range(5)

STATE_EMOJI = {
    "Available": "\U0001f7e2",      # green circle
    "Starting": "\U0001f7e1",       # yellow circle
    "ShuttingDown": "\U0001f7e0",   # orange circle
    "Shutdown": "\u26aa\ufe0f",     # white circle
}


def _ctx(context: ContextTypes.DEFAULT_TYPE):
    bd = context.application.bot_data
    return bd["settings"], bd["db"], bd["gh"], bd["keeper"]


async def _guard(update: Update, settings) -> bool:
    user = update.effective_user
    allowed = not settings.owner_ids or (user and user.id in settings.owner_ids)
    if allowed:
        return True
    if update.callback_query:
        await update.callback_query.answer("\u26d4\ufe0f Not allowed", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text("\u26d4\ufe0f You are not allowed to use this bot.")
    return False


async def _render(update: Update, text: str, keyboard: list[list[InlineKeyboardButton]]):
    markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except BadRequest:
            pass
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML
            )
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
    else:
        await update.effective_message.reply_text(
            text, reply_markup=markup, parse_mode=ParseMode.HTML
        )


def _arg(update: Update) -> str:
    return update.callback_query.data.split(":", 1)[1]


def _parse_hhmm(text: str) -> str | None:
    """Parse a 12h AM/PM or 24h time string; returns normalized 'HH:MM' or None."""
    s = text.strip()
    if not s or s == "-":
        return None

    # Try 12-hour formats
    for fmt in ("%I:%M %p", "%I:%M%p", "%I %p", "%I%p"):
        try:
            dt = datetime.strptime(s.upper(), fmt)
            return dt.strftime("%H:%M")
        except ValueError:
            pass

    # Try 24-hour formats
    parts = s.split(":")
    if len(parts) == 2:
        try:
            h, m = int(parts[0]), int(parts[1])
            if 0 <= h <= 23 and 0 <= m <= 59:
                return f"{h:02d}:{m:02d}"
        except ValueError:
            pass
    return None


def _fmt_hhmm(text: str | None) -> str:
    """Format stored 24h 'HH:MM' string into 12h AM/PM format (e.g. '02:30 PM')."""
    if not text or text in ("-", "(off)", "off"):
        return text or "(off)"
    parts = text.split(":")
    if len(parts) == 2:
        try:
            h, m = int(parts[0]), int(parts[1])
            t_obj = time(h, m)
            return t_obj.strftime("%I:%M %p")
        except ValueError:
            pass
    return text


def _fmt_ts(dt, tz_name: str, fmt: str = "%Y-%m-%d %I:%M:%S %p") -> str:
    """Render a stored UTC timestamp in the configured timezone (12h AM/PM by default)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz_name)).strftime(f"{fmt} %Z")


# ----------------------------------------------------------------------
# Main menu
# ----------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    text = (
        "\U0001f916 <b>Codespace Keeper</b>\n\n"
        "I keep your GitHub Codespaces alive by connecting over SSH and "
        "pinging them every 5 minutes.\n\n"
        "\u2022 Add any number of GitHub accounts\n"
        "\u2022 Manage all codespaces per account\n"
        "\u2022 Per-codespace startup commands\n"
        "\u2022 Start / stop keep-alive with one tap\n"
        "\u2022 Series: auto-switch to the next codespace on rate limit\n\n"
        "Prefer typing? Send /help for the full slash-command list."
    )
    keyboard = [
        [InlineKeyboardButton("\U0001f419 GitHub accounts", callback_data="accounts")],
        [InlineKeyboardButton("\U0001f501 Series (rate-limit failover)", callback_data="series")],
        [InlineKeyboardButton("\U0001f4e1 Keep-alive status", callback_data="status")],
    ]
    await _render(update, text, keyboard)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("cmds_cs_id", None)
    context.user_data.pop("sched_cs_id", None)
    await update.effective_message.reply_text("Cancelled. Send /start to open the menu.")
    return ConversationHandler.END


# ----------------------------------------------------------------------
# Accounts
# ----------------------------------------------------------------------

async def cb_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    accounts = await db.list_accounts(update.effective_user.id)
    rows = [
        [InlineKeyboardButton(f"\U0001f419 {a['login']}", callback_data=f"acct:{a['_id']}")]
        for a in accounts
    ]
    rows.append([InlineKeyboardButton("\u2795 Add account", callback_data="acct_add")])
    rows.append([InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="menu")])
    text = (
        "\U0001f419 <b>GitHub accounts</b>\n\n"
        + (f"{len(accounts)} account(s) stored." if accounts else "No accounts yet. Add one to get started.")
    )
    await _render(update, text, rows)


async def cb_acct_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    text = (
        "\u2795 <b>Add a GitHub account</b>\n\n"
        "\U0001f310 <b>Device login</b> — same flow as <code>gh auth login</code>: "
        "I give you a code, you enter it on github.com.\n\n"
        "\U0001f511 <b>Paste a token</b> — a personal access token with "
        "<code>repo</code> and <code>codespace</code> scopes."
    )
    keyboard = [
        [InlineKeyboardButton("\U0001f310 Device login (recommended)", callback_data="acct_add_device")],
        [InlineKeyboardButton("\U0001f511 Paste a token", callback_data="acct_add_token")],
        [InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="accounts")],
    ]
    await _render(update, text, keyboard)


async def cb_acct_add_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    q = update.callback_query
    await q.answer()
    try:
        flow = await gh.device_code_start()
    except Exception as exc:  # noqa: BLE001
        await _render(
            update,
            f"\u274c Could not start device login:\n<code>{html.escape(str(exc))}</code>",
            [[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="acct_add")]],
        )
        return

    uri = flow.get("verification_uri", "https://github.com/login/device")
    code = flow["user_code"]
    text = (
        "\U0001f310 <b>GitHub device login</b>\n\n"
        f"1. Open {html.escape(uri)}\n"
        f"2. Enter this code: <code>{html.escape(code)}</code>\n\n"
        "\u23f3 Waiting for you to authorize\u2026 (this message updates automatically)"
    )
    await _render(update, text, [[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="accounts")]])

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    bot = context.bot

    async def waiter():
        try:
            token = await gh.device_code_poll(
                flow["device_code"],
                int(flow.get("interval", 5)),
                int(flow.get("expires_in", 900)),
            )
            login = await gh.token_login(token)
            account = await db.add_account(user_id, login, token)
            await bot.send_message(
                chat_id,
                f"\u2705 GitHub account <b>{html.escape(login)}</b> added.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("\U0001f5a5 Open codespaces", callback_data=f"cslist:{account['_id']}")]]
                ),
            )
        except Exception as exc:  # noqa: BLE001
            await bot.send_message(
                chat_id,
                f"\u274c Device login failed: {html.escape(str(exc))}",
                parse_mode=ParseMode.HTML,
            )

    context.application.create_task(waiter())


async def cb_acct_add_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return ConversationHandler.END
    text = (
        "\U0001f511 <b>Paste your GitHub token</b>\n\n"
        "Create one at github.com \u2192 Settings \u2192 Developer settings \u2192 "
        "Personal access tokens (classic) with scopes "
        "<code>repo</code>, <code>codespace</code>.\n\n"
        "Send the token as a message now (it will be deleted right away), "
        "or /cancel."
    )
    await _render(update, text, [[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="acct_add")]])
    return ASK_TOKEN


async def msg_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return ConversationHandler.END
    token = (update.message.text or "").strip()
    try:
        await update.message.delete()  # don't leave the token in chat history
    except Exception:  # noqa: BLE001
        pass
    try:
        login = await gh.token_login(token)
    except Exception as exc:  # noqa: BLE001
        await update.effective_chat.send_message(
            f"\u274c {html.escape(str(exc))}\n\nTry again, or /cancel.",
            parse_mode=ParseMode.HTML,
        )
        return ASK_TOKEN
    account = await db.add_account(update.effective_user.id, login, token)
    await update.effective_chat.send_message(
        f"\u2705 GitHub account <b>{html.escape(login)}</b> added.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("\U0001f5a5 Open codespaces", callback_data=f"cslist:{account['_id']}")]]
        ),
    )
    return ConversationHandler.END


async def cb_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    account = await db.get_account(_arg(update))
    if not account:
        await cb_accounts(update, context)
        return
    key_status = (
        "stored \u2705" if account.get("ssh_private_key") else "auto-generated on first SSH connect"
    )
    text = (
        f"\U0001f419 <b>{html.escape(account['login'])}</b>\n\n"
        f"\U0001f510 SSH key: {key_status}"
    )
    keyboard = [
        [InlineKeyboardButton("\U0001f5a5 Codespaces", callback_data=f"cslist:{account['_id']}")],
        [InlineKeyboardButton("\U0001f511 SSH public key", callback_data=f"acctkey:{account['_id']}")],
        [InlineKeyboardButton("\U0001f5d1 Remove account", callback_data=f"acctdel:{account['_id']}")],
        [InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="accounts")],
    ]
    await _render(update, text, keyboard)


async def cb_acctkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    account = await db.get_account(_arg(update))
    if not account:
        await cb_accounts(update, context)
        return
    pub = account.get("ssh_public_key")
    body = (
        f"<pre>{html.escape(pub.strip())}</pre>"
        if pub
        else "No SSH key stored yet. It is generated automatically the first time I SSH into a codespace."
    )
    text = f"\U0001f511 <b>SSH public key — {html.escape(account['login'])}</b>\n\n{body}"
    await _render(
        update, text, [[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data=f"acct:{account['_id']}")]]
    )


async def cb_acctdel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    account = await db.get_account(_arg(update))
    if not account:
        await cb_accounts(update, context)
        return
    text = (
        f"\U0001f5d1 Remove <b>{html.escape(account['login'])}</b>?\n\n"
        "This stops its keep-alives and deletes the stored token, SSH keys "
        "and codespace settings."
    )
    keyboard = [
        [InlineKeyboardButton("\u2757 Yes, remove", callback_data=f"acctdelyes:{account['_id']}")],
        [InlineKeyboardButton("\u2b05\ufe0f Back", callback_data=f"acct:{account['_id']}")],
    ]
    await _render(update, text, keyboard)


async def cb_acctdelyes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    account_id = _arg(update)
    await keeper.stop_for_account(account_id)
    await db.delete_account(account_id)
    await cb_accounts(update, context)


# ----------------------------------------------------------------------
# Codespaces
# ----------------------------------------------------------------------

async def cb_cslist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    account = await db.get_account(_arg(update))
    if not account:
        await cb_accounts(update, context)
        return
    try:
        await update.callback_query.answer("Refreshing codespaces\u2026")
    except BadRequest:
        pass
    try:
        items = await gh.list_codespaces(account)
    except GhError as exc:
        await _render(
            update,
            f"\u274c Could not list codespaces:\n<code>{html.escape(str(exc)[:800])}</code>",
            [[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data=f"acct:{account['_id']}")]],
        )
        return
    rows = []
    for item in items:
        doc = await db.upsert_codespace(account["_id"], item)
        emoji = STATE_EMOJI.get(item.get("state", ""), "\u26aa\ufe0f")
        ka = " \U0001f501" if keeper.is_running(doc["_id"]) else ""
        label = f"{emoji}{ka} {doc['display_name']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"cs:{doc['_id']}")])
    rows.append([InlineKeyboardButton("\u2b05\ufe0f Back", callback_data=f"acct:{account['_id']}")])
    text = (
        f"\U0001f5a5 <b>Codespaces — {html.escape(account['login'])}</b>\n\n"
        + (f"{len(items)} codespace(s). \U0001f501 = keep-alive running." if items else "No codespaces found for this account.")
    )
    await _render(update, text, rows)


async def _show_cs(update: Update, context: ContextTypes.DEFAULT_TYPE, cs_id: str, live: bool = False):
    settings, db, gh, keeper = _ctx(context)
    cs = await db.get_codespace(cs_id)
    if not cs:
        await cb_accounts(update, context)
        return
    account = await db.get_account(cs["account_id"])
    if not account:
        await cb_accounts(update, context)
        return
    if live:
        try:
            state = await gh.get_codespace_state(account, cs["name"])
            await db.set_state(cs["_id"], state)
            cs["state"] = state
        except GhError:
            pass

    running = keeper.is_running(cs_id)
    emoji = STATE_EMOJI.get(cs.get("state", ""), "\u26aa\ufe0f")
    cmds = cs.get("startup_commands") or []
    last_ping = cs.get("last_ping")
    ping_line = "never"
    if last_ping:
        ok = "\u2705" if cs.get("last_ok") else "\u274c"
        ping_line = f"{_fmt_ts(last_ping, settings.schedule_tz)} {ok}"

    lines = [
        f"\U0001f5a5 <b>{html.escape(cs['display_name'])}</b>",
        "",
        f"\U0001f4c1 Repo: <code>{html.escape(cs.get('repository') or '?')}</code>",
        f"\U0001f419 Account: {html.escape(account['login'])}",
        f"{emoji} State: {html.escape(cs.get('state') or 'Unknown')}",
        f"\U0001f501 Keep-alive: {'<b>ON</b>' if running else 'off'}",
        f"\U0001f4e1 Last ping: {ping_line}",
    ]
    if cs.get("last_status"):
        lines.append(f"\U0001f4dd Status: {html.escape(cs['last_status'][:200])}")
    lines.append("")
    workdir = cs.get("startup_dir")
    if workdir:
        lines.append(f"\U0001f4c2 Startup directory: <code>{html.escape(workdir)}</code>")
    if cmds:
        lines.append("\u2699\ufe0f Startup commands:")
        lines.append("<pre>" + html.escape("\n".join(cmds)) + "</pre>")
    else:
        lines.append("\u2699\ufe0f Startup commands: none")
    stop_t, start_t = cs.get("schedule_stop"), cs.get("schedule_start")
    if stop_t or start_t:
        lines.append(
            f"\u23f0 Schedule ({html.escape(settings.schedule_tz)}): "
            f"stop {stop_t or 'off'} \u2022 start {start_t or 'off'}"
        )

    toggle = (
        InlineKeyboardButton("\u23f9 Stop (keep-alive + codespace)", callback_data=f"csstop:{cs['_id']}")
        if running
        else InlineKeyboardButton("\u25b6\ufe0f Start keep-alive", callback_data=f"csstart:{cs['_id']}")
    )
    keyboard = [
        [toggle],
        [InlineKeyboardButton("\u2699\ufe0f Startup commands", callback_data=f"cscmds:{cs['_id']}")],
        [InlineKeyboardButton("\u23f0 Auto start/stop", callback_data=f"cssched:{cs['_id']}")],
        [InlineKeyboardButton("\U0001f504 Refresh", callback_data=f"csrefresh:{cs['_id']}")],
        [InlineKeyboardButton("\u2b05\ufe0f Back", callback_data=f"cslist:{account['_id']}")],
    ]
    await _render(update, "\n".join(lines), keyboard)


async def cb_cs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    await _show_cs(update, context, _arg(update))


async def cb_csrefresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    await _show_cs(update, context, _arg(update), live=True)


async def cb_csstart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    cs_id = _arg(update)
    await keeper.start(cs_id)
    try:
        await update.callback_query.answer("\u25b6\ufe0f Keep-alive started")
    except BadRequest:
        pass
    await _show_cs(update, context, cs_id)


async def cb_csstop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    cs_id = _arg(update)
    try:
        await update.callback_query.answer(
            "\u23f9 Stopping keep-alive and shutting the codespace down\u2026"
        )
    except BadRequest:
        pass
    await keeper.stop_and_shutdown(cs_id)
    await _show_cs(update, context, cs_id)


async def cb_cscmds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    cs_id = _arg(update)
    cs = await db.get_codespace(cs_id)
    if not cs:
        return
    workdir = cs.get("startup_dir") or "(none \u2014 home directory)"
    current = "\n".join(cs.get("startup_commands") or []) or "(none)"
    text = (
        f"\u2699\ufe0f <b>Startup setup — {html.escape(cs['display_name'])}</b>\n\n"
        "These run inside the codespace every time it starts.\n\n"
        f"\U0001f4c2 <b>1. Directory (cd \u2026):</b>\n<pre>{html.escape(workdir)}</pre>\n"
        f"\u26a1 <b>2. Sh command(s):</b>\n<pre>{html.escape(current)}</pre>\n\n"
        "Set the directory first (like <code>~</code> or <code>~/mydirectory</code>), "
        "then the sh command(s) that run there."
    )
    keyboard = [
        [InlineKeyboardButton("\U0001f4c2 1. Set directory (cd \u2026)", callback_data=f"csdir:{cs_id}")],
        [InlineKeyboardButton("\u26a1 2. Set sh command(s)", callback_data=f"cssh:{cs_id}")],
        [InlineKeyboardButton("\U0001f9f9 Clear all", callback_data=f"cscmdclear:{cs_id}")],
        [InlineKeyboardButton("\u2b05\ufe0f Back", callback_data=f"cs:{cs_id}")],
    ]
    await _render(update, text, keyboard)


async def cb_csdir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return ConversationHandler.END
    cs_id = _arg(update)
    cs = await db.get_codespace(cs_id)
    if not cs:
        return ConversationHandler.END
    context.user_data["cmds_cs_id"] = cs_id
    current = cs.get("startup_dir") or "(none)"
    text = (
        f"\U0001f4c2 <b>Step 1/2: directory — {html.escape(cs['display_name'])}</b>\n\n"
        f"Current: <code>{html.escape(current)}</code>\n\n"
        "Send the directory to <code>cd</code> into before running the sh "
        "command(s), e.g. <code>~</code>, <code>~/mydirectory</code> or "
        "<code>/workspaces/myrepo</code>. (Sending <code>cd ~/mydirectory</code> "
        "also works.)\n"
        "Send <code>-</code> for no directory (home), or /cancel to keep it."
    )
    await _render(update, text, [[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data=f"cscmds:{cs_id}")]])
    return ASK_DIR


async def msg_csdir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return ConversationHandler.END
    cs_id = context.user_data.get("cmds_cs_id")
    if not cs_id:
        return ConversationHandler.END
    raw = (update.message.text or "").strip()
    if raw.startswith("cd "):
        raw = raw[3:].strip()
    workdir = None if raw in ("-", "") else raw
    await db.set_startup_dir(cs_id, workdir)
    saved = f"<code>{html.escape(workdir)}</code>" if workdir else "(none \u2014 home directory)"
    await update.message.reply_text(
        f"\u2705 Directory saved: {saved}\n\n"
        "\u26a1 <b>Step 2/2:</b> now send the sh command(s) to run there, "
        "<b>one per line</b> (e.g. <code>sh start.sh</code>).\n"
        "For background daemons use <code>nohup ./server &amp;</code>.\n"
        "Send <code>-</code> to clear the commands, or /cancel to keep the current ones.",
        parse_mode=ParseMode.HTML,
    )
    return ASK_CMDS


async def cb_cssh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return ConversationHandler.END
    cs_id = _arg(update)
    cs = await db.get_codespace(cs_id)
    if not cs:
        return ConversationHandler.END
    context.user_data["cmds_cs_id"] = cs_id
    current = "\n".join(cs.get("startup_commands") or []) or "(none)"
    workdir = cs.get("startup_dir")
    where = f"<code>{html.escape(workdir)}</code>" if workdir else "the home directory"
    text = (
        f"\u26a1 <b>Step 2/2: sh command(s) — {html.escape(cs['display_name'])}</b>\n\n"
        f"They will run in {where}.\n\n"
        f"Current:\n<pre>{html.escape(current)}</pre>\n\n"
        "Send the command(s) now, <b>one per line</b> (e.g. <code>sh start.sh</code>).\n"
        "For background daemons use <code>nohup ./server &amp;</code>.\n"
        "Send <code>-</code> to clear, or /cancel to keep the current ones."
    )
    await _render(update, text, [[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data=f"cscmds:{cs_id}")]])
    return ASK_CMDS


async def msg_cscmds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return ConversationHandler.END
    cs_id = context.user_data.pop("cmds_cs_id", None)
    if not cs_id:
        return ConversationHandler.END
    raw = (update.message.text or "").strip()
    commands = [] if raw == "-" else [line.strip() for line in raw.splitlines() if line.strip()]
    await db.set_startup_commands(cs_id, commands)
    summary = "cleared" if not commands else f"saved ({len(commands)} command(s))"
    await update.message.reply_text(
        f"\u2705 Startup commands {summary}.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("\u2b05\ufe0f Back to codespace", callback_data=f"cs:{cs_id}")]]
        ),
    )
    return ConversationHandler.END


async def cb_cscmdclear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    cs_id = _arg(update)
    await db.set_startup_commands(cs_id, [])
    await db.set_startup_dir(cs_id, None)
    try:
        await update.callback_query.answer("\U0001f9f9 Cleared")
    except BadRequest:
        pass
    await _show_cs(update, context, cs_id)


# ----------------------------------------------------------------------
# Auto start/stop schedule
# ----------------------------------------------------------------------

async def cb_cssched(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    cs_id = _arg(update)
    cs = await db.get_codespace(cs_id)
    if not cs:
        return
    stop_t = _fmt_hhmm(cs.get("schedule_stop"))
    start_t = _fmt_hhmm(cs.get("schedule_start"))
    tz = html.escape(settings.schedule_tz)
    text = (
        f"\u23f0 <b>Auto start/stop — {html.escape(cs['display_name'])}</b>\n\n"
        f"Times are daily in <b>{tz}</b>.\n\n"
        f"\U0001f6d1 Stop at: <code>{html.escape(stop_t)}</code>\n"
        f"\u25b6\ufe0f Start at: <code>{html.escape(start_t)}</code>\n\n"
        "At stop time I shut the codespace down and pause keep-alive. At "
        "start time I boot it again, run the startup commands and resume "
        "keep-alive if it was on."
    )
    keyboard = [
        [InlineKeyboardButton("\U0001f6d1 Set stop time", callback_data=f"csstopt:{cs_id}")],
        [InlineKeyboardButton("\u25b6\ufe0f Set start time", callback_data=f"csstartt:{cs_id}")],
        [InlineKeyboardButton("\U0001f9f9 Clear schedule", callback_data=f"csschedclear:{cs_id}")],
        [InlineKeyboardButton("\u2b05\ufe0f Back", callback_data=f"cs:{cs_id}")],
    ]
    await _render(update, text, keyboard)


async def cb_csstopt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return ConversationHandler.END
    cs_id = _arg(update)
    cs = await db.get_codespace(cs_id)
    if not cs:
        return ConversationHandler.END
    context.user_data["sched_cs_id"] = cs_id
    tz = html.escape(settings.schedule_tz)
    text = (
        f"\U0001f6d1 <b>Daily STOP time — {html.escape(cs['display_name'])}</b>\n\n"
        f"Current: <code>{html.escape(_fmt_hhmm(cs.get('schedule_stop')))}</code>\n\n"
        f"Send the time to stop the codespace every day in <b>{tz}</b> "
        "(e.g. <code>11:30 PM</code> or <code>23:30</code>).\n"
        "Send <code>-</code> to disable auto-stop, or /cancel to keep it."
    )
    await _render(update, text, [[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data=f"cssched:{cs_id}")]])
    return ASK_STOP_TIME


async def msg_stop_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return ConversationHandler.END
    cs_id = context.user_data.get("sched_cs_id")
    if not cs_id:
        return ConversationHandler.END
    raw = (update.message.text or "").strip()
    if raw == "-":
        value = None
    else:
        value = _parse_hhmm(raw)
        if value is None:
            await update.message.reply_text(
                "\u274c Invalid time. Send e.g. 11:30 PM or 23:30, - to disable, or /cancel."
            )
            return ASK_STOP_TIME
    if cs_id == "series":
        await db.save_series({"schedule_stop": value})
    else:
        await db.update_codespace_fields(cs_id, {"schedule_stop": value})
    saved = _fmt_hhmm(value) if value else "disabled"
    await update.message.reply_text(
        f"\u2705 Daily stop time: <b>{saved}</b>\n\n"
        "\u25b6\ufe0f Now send the daily START time (e.g. <code>07:00 AM</code> or <code>07:00</code>), "
        "<code>-</code> to disable auto-start, or /cancel to finish.",
        parse_mode=ParseMode.HTML,
    )
    return ASK_START_TIME


async def cb_csstartt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return ConversationHandler.END
    cs_id = _arg(update)
    cs = await db.get_codespace(cs_id)
    if not cs:
        return ConversationHandler.END
    context.user_data["sched_cs_id"] = cs_id
    tz = html.escape(settings.schedule_tz)
    text = (
        f"\u25b6\ufe0f <b>Daily START time — {html.escape(cs['display_name'])}</b>\n\n"
        f"Current: <code>{html.escape(_fmt_hhmm(cs.get('schedule_start')))}</code>\n\n"
        f"Send the time to start the codespace every day in <b>{tz}</b> "
        "(e.g. <code>07:00 AM</code> or <code>07:00</code>).\n"
        "Send <code>-</code> to disable auto-start, or /cancel to keep it."
    )
    await _render(update, text, [[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data=f"cssched:{cs_id}")]])
    return ASK_START_TIME


async def msg_start_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return ConversationHandler.END
    cs_id = context.user_data.pop("sched_cs_id", None)
    if not cs_id:
        return ConversationHandler.END
    raw = (update.message.text or "").strip()
    if raw == "-":
        value = None
    else:
        value = _parse_hhmm(raw)
        if value is None:
            context.user_data["sched_cs_id"] = cs_id
            await update.message.reply_text(
                "\u274c Invalid time. Send e.g. 07:00 AM or 07:00, - to disable, or /cancel."
            )
            return ASK_START_TIME
    if cs_id == "series":
        await db.save_series({"schedule_start": value})
        series = await db.get_series()
        stop_t = _fmt_hhmm(series.get("schedule_stop"))
        label = "Series schedule"
        back = InlineKeyboardButton("\u2b05\ufe0f Back to series", callback_data="series")
    else:
        await db.update_codespace_fields(cs_id, {"schedule_start": value})
        cs = await db.get_codespace(cs_id)
        stop_t = _fmt_hhmm(cs.get("schedule_stop") if cs else None)
        label = "Schedule"
        back = InlineKeyboardButton("\u2b05\ufe0f Back to codespace", callback_data=f"cs:{cs_id}")
    await update.message.reply_text(
        f"\u23f0 {label} saved ({settings.schedule_tz}):\n"
        f"\U0001f6d1 Stop daily at: <b>{stop_t}</b>\n"
        f"\u25b6\ufe0f Start daily at: <b>{_fmt_hhmm(value)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[back]]),
    )
    return ConversationHandler.END


async def cb_csschedclear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    cs_id = _arg(update)
    await db.update_codespace_fields(
        cs_id,
        {"schedule_stop": None, "schedule_start": None, "keepalive_resume": False},
    )
    try:
        await update.callback_query.answer("\U0001f9f9 Schedule cleared")
    except BadRequest:
        pass
    await _show_cs(update, context, cs_id)


# ----------------------------------------------------------------------
# Series: rate-limit failover rotation
# ----------------------------------------------------------------------

async def _series_text_kb(db, keeper, settings):
    series = await db.get_series()
    cs_ids = [str(x) for x in (series.get("cs_ids") or [])]
    running = bool(series.get("running")) and keeper.series_running()
    active = str(series.get("active")) if series.get("active") else None
    accounts = {str(a["_id"]): a for a in await db.all_accounts()}
    all_cs = await db.all_codespaces()
    status = "\U0001f7e2 running" if running else "\u26aa\ufe0f stopped"
    lines = [
        "\U0001f501 <b>Series (rate-limit failover)</b>\n",
        "Codespaces run one at a time, in order. When the active one replies "
        "with a GitHub rate-limit error, I stop it and start the next in the "
        "series — looping back to the first. Auto start/stop schedules apply "
        "to series codespaces too.\n",
        f"Status: <b>{status}</b>",
    ]
    sched_stop = series.get("schedule_stop")
    sched_start = series.get("schedule_start")
    if sched_stop or sched_start:
        lines.append(
            f"\u23f0 Schedule ({html.escape(settings.schedule_tz)}): "
            f"stop {_fmt_hhmm(sched_stop)} \u2022 start {_fmt_hhmm(sched_start)}"
        )
    if cs_ids:
        lines.append("Order:")
        for i, cid in enumerate(cs_ids, 1):
            cs = next((c for c in all_cs if str(c["_id"]) == cid), None)
            if not cs:
                continue
            acct = accounts.get(str(cs["account_id"]))
            alias = (acct.get("alias") or acct.get("login")) if acct else "?"
            marker = " \u25b6\ufe0f active" if (running and cid == active) else ""
            lines.append(
                f"  {i}. {html.escape(str(alias))} / "
                f"{html.escape(cs['display_name'])}{marker}"
            )
    else:
        lines.append("No codespaces selected yet.")
    lines.append("\nTap a codespace to add/remove it (order = tap order):")
    keyboard = []
    for cs in all_cs:
        cid = str(cs["_id"])
        acct = accounts.get(str(cs["account_id"]))
        alias = (acct.get("alias") or acct.get("login")) if acct else "?"
        if cid in cs_ids:
            label = f"\u2705 {cs_ids.index(cid) + 1}. {alias} / {cs['display_name']}"
        else:
            label = f"\u25ab\ufe0f {alias} / {cs['display_name']}"
        keyboard.append(
            [InlineKeyboardButton(label[:60], callback_data=f"sersel:{cid}")]
        )
    toggle = (
        InlineKeyboardButton("\u23f9 Stop series", callback_data="serstop")
        if running
        else InlineKeyboardButton("\u25b6\ufe0f Start series", callback_data="serstart")
    )
    keyboard.append([toggle])
    keyboard.append([InlineKeyboardButton("\u23f0 Series schedule", callback_data="sersched")])
    keyboard.append([InlineKeyboardButton("\U0001f9f9 Clear series", callback_data="serclear")])
    keyboard.append([InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="menu")])
    return "\n".join(lines), keyboard


async def cb_series(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    text, keyboard = await _series_text_kb(db, keeper, settings)
    await _render(update, text, keyboard)


async def cb_sersel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    cs_id = _arg(update)
    series = await db.get_series()
    cs_ids = [str(x) for x in (series.get("cs_ids") or [])]
    if cs_id in cs_ids:
        cs_ids.remove(cs_id)
    else:
        cs_ids.append(cs_id)
    fields: dict = {"cs_ids": cs_ids}
    if str(series.get("active")) not in cs_ids:
        fields["active"] = cs_ids[0] if cs_ids else None
    await db.save_series(fields)
    if not cs_ids and (series.get("running") or keeper.series_running()):
        await keeper.stop_series()
    await cb_series(update, context)


async def cb_serstart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    ok = await keeper.start_series()
    try:
        await update.callback_query.answer(
            "\u25b6\ufe0f Series started" if ok else "Select at least one codespace first"
        )
    except BadRequest:
        pass
    await cb_series(update, context)


async def cb_serstop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    await keeper.stop_series()
    try:
        await update.callback_query.answer("\u23f9 Series stopped")
    except BadRequest:
        pass
    await cb_series(update, context)


async def cb_serclear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    await keeper.stop_series()
    await db.save_series(
        {"cs_ids": [], "active": None, "resume": False, "running": False}
    )
    try:
        await update.callback_query.answer("\U0001f9f9 Series cleared")
    except BadRequest:
        pass
    await cb_series(update, context)


async def cb_sersched(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    series = await db.get_series()
    stop_t = series.get("schedule_stop") or "(off)"
    start_t = series.get("schedule_start") or "(off)"
    tz = html.escape(settings.schedule_tz)
    text = (
        "\u23f0 <b>Series schedule</b>\n\n"
        f"Daily times, 24h <code>HH:MM</code> in <b>{tz}</b> \u2014 they "
        "apply to the series as a whole.\n\n"
        f"\U0001f6d1 Stop at: <code>{html.escape(stop_t)}</code>\n"
        f"\u25b6\ufe0f Start at: <code>{html.escape(start_t)}</code>\n\n"
        "At stop time I pause the series and shut the active codespace "
        "down. At start time I start the series again \u2014 it resumes from "
        "the codespace that was active and keeps rotating on rate limits."
    )
    keyboard = [
        [InlineKeyboardButton("\U0001f6d1 Set stop time", callback_data="serstopt")],
        [InlineKeyboardButton("\u25b6\ufe0f Set start time", callback_data="serstartt")],
        [InlineKeyboardButton("\U0001f9f9 Clear schedule", callback_data="serschedclear")],
        [InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="series")],
    ]
    await _render(update, text, keyboard)


async def cb_serstopt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return ConversationHandler.END
    series = await db.get_series()
    context.user_data["sched_cs_id"] = "series"
    tz = html.escape(settings.schedule_tz)
    text = (
        "\U0001f6d1 <b>Daily STOP time \u2014 series</b>\n\n"
        f"Current: <code>{html.escape(series.get('schedule_stop') or '(off)')}</code>\n\n"
        f"Send the time to stop the series every day, 24h <code>HH:MM</code> "
        f"in <b>{tz}</b> (e.g. <code>23:30</code>).\n"
        "Send <code>-</code> to disable, or /cancel to keep it."
    )
    await _render(update, text, [[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="sersched")]])
    return ASK_STOP_TIME


async def cb_serstartt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return ConversationHandler.END
    series = await db.get_series()
    context.user_data["sched_cs_id"] = "series"
    tz = html.escape(settings.schedule_tz)
    text = (
        "\u25b6\ufe0f <b>Daily START time \u2014 series</b>\n\n"
        f"Current: <code>{html.escape(series.get('schedule_start') or '(off)')}</code>\n\n"
        f"Send the time to start the series every day, 24h <code>HH:MM</code> "
        f"in <b>{tz}</b> (e.g. <code>07:00</code>).\n"
        "Send <code>-</code> to disable, or /cancel to keep it."
    )
    await _render(update, text, [[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="sersched")]])
    return ASK_START_TIME


async def cb_serschedclear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    await db.save_series({"schedule_stop": None, "schedule_start": None})
    try:
        await update.callback_query.answer("\U0001f9f9 Series schedule cleared")
    except BadRequest:
        pass
    await cb_series(update, context)


# ----------------------------------------------------------------------
# Status
# ----------------------------------------------------------------------

async def cb_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, gh, keeper = _ctx(context)
    if not await _guard(update, settings):
        return
    docs = await db.list_keepalive()
    if not docs:
        text = "\U0001f4e1 <b>Keep-alive status</b>\n\nNo keep-alives are running."
    else:
        lines = ["\U0001f4e1 <b>Keep-alive status</b>", ""]
        for cs in docs:
            account = await db.get_account(cs["account_id"])
            login = account["login"] if account else "?"
            running = "\U0001f501" if keeper.is_running(cs["_id"]) else "\u26a0\ufe0f not running"
            last_ping = cs.get("last_ping")
            when = (
                _fmt_ts(last_ping, settings.schedule_tz, "%H:%M:%S")
                if last_ping
                else "never"
            )
            ok = "\u2705" if cs.get("last_ok") else ("\u274c" if last_ping else "")
            lines.append(
                f"{running} <b>{html.escape(cs['display_name'])}</b> "
                f"({html.escape(login)}) — last ping {when} {ok}"
            )
        text = "\n".join(lines)
    await _render(update, text, [[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="menu")]])


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------

def register(app: Application) -> None:
    conv_token = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_acct_add_token, pattern=r"^acct_add_token$")],
        states={ASK_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_token)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    conv_cmds = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_csdir, pattern=r"^csdir:"),
            CallbackQueryHandler(cb_cssh, pattern=r"^cssh:"),
        ],
        states={
            ASK_DIR: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_csdir)],
            ASK_CMDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_cscmds)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    conv_sched = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_csstopt, pattern=r"^csstopt:"),
            CallbackQueryHandler(cb_csstartt, pattern=r"^csstartt:"),
            CallbackQueryHandler(cb_serstopt, pattern=r"^serstopt$"),
            CallbackQueryHandler(cb_serstartt, pattern=r"^serstartt$"),
        ],
        states={
            ASK_STOP_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_stop_time)],
            ASK_START_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_start_time)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv_token)
    app.add_handler(conv_cmds)
    app.add_handler(conv_sched)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    app.add_handler(CallbackQueryHandler(cmd_start, pattern=r"^menu$"))
    app.add_handler(CallbackQueryHandler(cb_status, pattern=r"^status$"))
    app.add_handler(CallbackQueryHandler(cb_accounts, pattern=r"^accounts$"))
    app.add_handler(CallbackQueryHandler(cb_acct_add, pattern=r"^acct_add$"))
    app.add_handler(CallbackQueryHandler(cb_acct_add_device, pattern=r"^acct_add_device$"))
    app.add_handler(CallbackQueryHandler(cb_account, pattern=r"^acct:"))
    app.add_handler(CallbackQueryHandler(cb_acctkey, pattern=r"^acctkey:"))
    app.add_handler(CallbackQueryHandler(cb_acctdel, pattern=r"^acctdel:"))
    app.add_handler(CallbackQueryHandler(cb_acctdelyes, pattern=r"^acctdelyes:"))
    app.add_handler(CallbackQueryHandler(cb_cslist, pattern=r"^cslist:"))
    app.add_handler(CallbackQueryHandler(cb_cs, pattern=r"^cs:"))
    app.add_handler(CallbackQueryHandler(cb_csrefresh, pattern=r"^csrefresh:"))
    app.add_handler(CallbackQueryHandler(cb_csstart, pattern=r"^csstart:"))
    app.add_handler(CallbackQueryHandler(cb_csstop, pattern=r"^csstop:"))
    app.add_handler(CallbackQueryHandler(cb_cscmds, pattern=r"^cscmds:"))
    app.add_handler(CallbackQueryHandler(cb_cscmdclear, pattern=r"^cscmdclear:"))
    app.add_handler(CallbackQueryHandler(cb_cssched, pattern=r"^cssched:"))
    app.add_handler(CallbackQueryHandler(cb_csschedclear, pattern=r"^csschedclear:"))
    app.add_handler(CallbackQueryHandler(cb_series, pattern=r"^series$"))
    app.add_handler(CallbackQueryHandler(cb_sersel, pattern=r"^sersel:"))
    app.add_handler(CallbackQueryHandler(cb_serstart, pattern=r"^serstart$"))
    app.add_handler(CallbackQueryHandler(cb_serstop, pattern=r"^serstop$"))
    app.add_handler(CallbackQueryHandler(cb_serclear, pattern=r"^serclear$"))
    app.add_handler(CallbackQueryHandler(cb_sersched, pattern=r"^sersched$"))
    app.add_handler(CallbackQueryHandler(cb_serschedclear, pattern=r"^serschedclear$"))
