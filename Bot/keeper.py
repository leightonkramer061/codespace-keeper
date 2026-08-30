"""Keep-alive worker manager + codespace start watcher.

Keep-alive: for every codespace with keep-alive enabled, an asyncio task
connects over SSH every KEEPALIVE_INTERVAL seconds (default: 5 minutes) and
runs a tiny command. The SSH activity resets GitHub's idle timeout so the
codespace is never stopped.

Watcher: ONE background task polls the state of EVERY tracked codespace that
has startup commands (every WATCH_INTERVAL seconds, default 60). The moment a
codespace transitions to "Available" -- no matter when or where it was
started (this bot, github.com, VS Code, gh CLI) -- the configured startup
commands are executed once for that session. A per-codespace lock plus the
persisted state in MongoDB guarantee the commands never run twice for the
same boot, even when the keep-alive loop and the watcher race.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

PING_COMMAND = "echo codespace-keeper-ping"

# Substrings that identify a GitHub rate-limit reply (checked lowercase).
# GitHub wording varies: "API rate limit exceeded", "You have exceeded a
# secondary rate limit", "rate limited", HTTP 429 "too many requests", ...
RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "rate limited",
    "too many requests",
    "quota exhausted",
    "quota exceeded",
    "abuse detection",
    "http 429",
    "error 429",
    "status 429",
    "(429)",
)


def _is_rate_limited(text: str) -> bool:
    t = (text or "").lower()
    return any(marker in t for marker in RATE_LIMIT_MARKERS)


def build_startup_command(cmd: str, workdir: str | None = None) -> str:
    """Wrap command for reliable execution inside Codespace Python/Linux environments.
    
    Handles:
    1. Sourcing login/profile scripts and user .bashrc for environment variables.
    2. Adding common Python paths (~/.local/bin, /usr/local/python/..., conda) to PATH.
    3. Changing to the configured directory or auto-detecting the repo in /workspaces.
    4. Auto-activating Python virtual environments (.venv, venv, env) if present.
    5. Aliasing python -> python3 if python binary is missing.
    6. Properly detaching background processes (& or nohup) with output redirected
       to /tmp/codespace_startup.log so SSH sessions never hang or get killed.
    """
    clean_cmd = cmd.strip()
    is_bg = clean_cmd.endswith("&")
    if is_bg:
        clean_cmd = clean_cmd[:-1].strip()

    script_parts = [
        "#!/usr/bin/env bash",
        "[ -f /etc/profile ] && . /etc/profile >/dev/null 2>&1 || true",
        "[ -f ~/.profile ] && . ~/.profile >/dev/null 2>&1 || true",
        "[ -f ~/.bashrc ] && . ~/.bashrc >/dev/null 2>&1 || true",
        'export PATH="$HOME/.local/bin:$HOME/.python/current/bin:/usr/local/python/current/bin:/usr/local/py-utils/bin:/opt/conda/bin:$PATH"',
    ]
    if workdir:
        script_parts.append(f'cd {shlex.quote(workdir)} 2>/dev/null || cd ~')
    else:
        script_parts.append(
            'if [ -d "/workspaces" ]; then\n'
            '    for _d in /workspaces/*; do\n'
            '        if [ -d "$_d" ]; then\n'
            '            cd "$_d"\n'
            '            break\n'
            '        fi\n'
            '    done\n'
            'fi'
        )

    script_parts.append(
        'if [ -z "$VIRTUAL_ENV" ]; then\n'
        '    if [ -f ".venv/bin/activate" ]; then\n'
        '        . .venv/bin/activate\n'
        '    elif [ -f "venv/bin/activate" ]; then\n'
        '        . venv/bin/activate\n'
        '    elif [ -f "env/bin/activate" ]; then\n'
        '        . env/bin/activate\n'
        '    fi\n'
        'fi\n'
        'if ! command -v python >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then\n'
        '    python() { python3 "$@"; }\n'
        '    export -f python 2>/dev/null || true\n'
        'fi'
    )

    if is_bg:
        nohup_cmd = clean_cmd if clean_cmd.startswith("nohup ") else f"nohup {clean_cmd}"
        script_parts.append(
            f'mkdir -p /tmp && ( {nohup_cmd} </dev/null >>/tmp/codespace_startup.log 2>&1 & ) && disown -a 2>/dev/null || true'
        )
    else:
        script_parts.append(clean_cmd)

    return "\n".join(script_parts)


class KeeperManager:
    def __init__(self, settings, db, gh) -> None:
        self.settings = settings
        self.db = db
        self.gh = gh
        self.tasks: dict[str, asyncio.Task] = {}
        self.watch_task: asyncio.Task | None = None
        self.sched_task: asyncio.Task | None = None
        self.series_task: asyncio.Task | None = None
        self._startup_locks: dict[str, asyncio.Lock] = {}

    def is_running(self, cs_id) -> bool:
        task = self.tasks.get(str(cs_id))
        return task is not None and not task.done()

    async def start(self, cs_id) -> bool:
        key = str(cs_id)
        if self.is_running(key):
            return False
        await self.db.set_keepalive(key, True)
        self.tasks[key] = asyncio.create_task(self._loop(key), name=f"keeper-{key}")
        log.info("Keep-alive started for codespace %s", key)
        return True

    async def stop(self, cs_id) -> bool:
        key = str(cs_id)
        await self.db.set_keepalive(key, False)
        task = self.tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            log.info("Keep-alive stopped for codespace %s", key)
            return True
        return False

    async def stop_and_shutdown(self, cs_id) -> bool:
        """Stop the keep-alive AND shut the codespace itself down on GitHub."""
        key = str(cs_id)
        await self.stop(key)
        await self.db.set_startup_done(key, False)
        cs = await self.db.get_codespace(key)
        account = await self.db.get_account(cs["account_id"]) if cs else None
        if not cs or not account:
            return False
        try:
            await self.gh.stop_codespace(account, cs["name"])
            await self.db.set_state(key, "Shutdown")
            await self.db.record_ping(key, True, "stopped by user")
            log.info("Codespace %s shut down by user", cs["name"])
            return True
        except Exception as exc:  # noqa: BLE001
            await self.db.record_ping(key, False, f"stop failed: {exc}"[:400])
            log.exception("Could not shut down codespace %s", cs["name"])
            return False

    async def stop_for_account(self, account_id) -> None:
        for cs in await self.db.list_codespaces(account_id):
            await self.stop(cs["_id"])

    async def restore(self) -> None:
        """Re-start keep-alive tasks after a bot restart."""
        for cs in await self.db.list_keepalive():
            key = str(cs["_id"])
            if not self.is_running(key):
                self.tasks[key] = asyncio.create_task(
                    self._loop(key), name=f"keeper-{key}"
                )
                log.info("Restored keep-alive for codespace %s", cs.get("display_name"))

    def start_watcher(self) -> None:
        """Start the global codespace start watcher (idempotent)."""
        if self.watch_task is None or self.watch_task.done():
            self.watch_task = asyncio.create_task(
                self._watch_loop(), name="keeper-watcher"
            )
            log.info(
                "Start watcher running: startup commands fire whenever a "
                "codespace starts (checked every %ss)",
                self.settings.watch_interval,
            )

    def start_scheduler(self) -> None:
        """Start the daily auto start/stop scheduler (idempotent)."""
        if self.sched_task is None or self.sched_task.done():
            self.sched_task = asyncio.create_task(
                self._sched_loop(), name="keeper-scheduler"
            )
            log.info(
                "Auto start/stop scheduler running (timezone: %s)",
                self.settings.schedule_tz,
            )

    # ------------------------------------------------------------------
    # Series: rate-limit failover rotation
    # ------------------------------------------------------------------

    def series_running(self) -> bool:
        return self.series_task is not None and not self.series_task.done()

    async def restore_series(self) -> None:
        """Resume a running series after a bot restart."""
        series = await self.db.get_series()
        if series.get("running") and series.get("cs_ids"):
            await self.start_series()
            log.info("Restored series rotation after restart")

    async def start_series(self, active_cs_id: str | None = None) -> bool:
        series = await self.db.get_series()
        cs_ids = [str(x) for x in (series.get("cs_ids") or [])]
        if not cs_ids:
            return False
        active = (
            str(active_cs_id)
            if active_cs_id
            else str(series.get("active") or cs_ids[0])
        )
        if active not in cs_ids:
            active = cs_ids[0]
        # The series manages its own pings -> pause plain keep-alive tasks
        # for its members so the two don't fight over start/stop.
        for cs_id in cs_ids:
            if self.is_running(cs_id):
                await self.stop(cs_id)
        await self.db.save_series(
            {"running": True, "active": active, "resume": False}
        )
        if not self.series_running():
            self.series_task = asyncio.create_task(
                self._series_loop(), name="keeper-series"
            )
        log.info(
            "Series rotation started (%d codespaces, active: %s)",
            len(cs_ids),
            active,
        )
        return True

    async def stop_series(self, *, shutdown_active: bool = True) -> bool:
        series = await self.db.get_series()
        await self.db.save_series({"running": False})
        task = self.series_task
        self.series_task = None
        stopped = False
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            log.info("Series rotation stopped")
            stopped = True
        # Stopping the series also shuts the active codespace down, so
        # nothing keeps running (and burning quota) in the background.
        if shutdown_active and series.get("active"):
            cs = await self.db.get_codespace(str(series["active"]))
            account = await self.db.get_account(cs["account_id"]) if cs else None
            if cs and account:
                try:
                    await self.gh.stop_codespace(account, cs["name"])
                    await self.db.set_state(str(cs["_id"]), "Shutdown")
                    await self.db.set_startup_done(str(cs["_id"]), False)
                    await self.db.record_ping(
                        str(cs["_id"]), True, "series stopped"
                    )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "Could not shut down series codespace %s", cs["name"]
                    )
        return stopped

    async def _series_loop(self) -> None:
        interval = self.settings.keepalive_interval
        while True:
            delay = interval
            try:
                series = await self.db.get_series()
                cs_ids = [str(x) for x in (series.get("cs_ids") or [])]
                if not series.get("running") or not cs_ids:
                    return
                active = str(series.get("active") or cs_ids[0])
                if active not in cs_ids:
                    active = cs_ids[0]
                    await self.db.save_series({"active": active})
                cs = await self.db.get_codespace(active)
                account = (
                    await self.db.get_account(cs["account_id"]) if cs else None
                )
                if not cs or not account:
                    # Codespace/account was deleted -> drop it from the series.
                    cs_ids = [c for c in cs_ids if c != active]
                    await self.db.save_series(
                        {"cs_ids": cs_ids, "active": cs_ids[0] if cs_ids else None}
                    )
                    if not cs_ids:
                        await self.db.save_series({"running": False})
                        return
                    continue
                name = cs["name"]
                prev = cs.get("state")
                try:
                    # SSH connect boots the codespace if it is stopped and
                    # counts as activity (resets GitHub's idle timer).
                    rc, out = await self.gh.ssh_exec(account, name, PING_COMMAND)
                    if rc != 0 and not _is_rate_limited(out):
                        try:
                            await self.gh.start_codespace(account, name)
                            rc, out = 0, "api start fallback"
                        except Exception:
                            pass
                except Exception as exc:  # noqa: BLE001 - includes GhError
                    rc, out = 1, str(exc)
                if rc == 0:
                    await self.db.record_ping(active, True, "series ping ok")
                    refreshed = await self.db.get_codespace(active)
                    if refreshed and (not refreshed.get("startup_done") or prev != "Available"):
                        await self.run_startup_commands(
                            active, reason="series start"
                        )
                elif _is_rate_limited(out):
                    idx = cs_ids.index(active)
                    nxt = cs_ids[(idx + 1) % len(cs_ids)]
                    await self.db.record_ping(
                        active,
                        False,
                        f"rate limited -> switching to next in series: "
                        f"{(out or '')[-200:]}",
                    )
                    log.warning(
                        "Series: %s hit a rate limit, switching to next", name
                    )
                    try:
                        await self.gh.stop_codespace(account, name)
                        await self.db.set_state(active, "Shutdown")
                        await self.db.set_startup_done(active, False)
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "Series: failed to stop rate-limited %s", name
                        )
                    await self.db.save_series({"active": nxt})
                    delay = 5  # bring the next codespace up right away
                else:
                    await self.db.record_ping(
                        active, False, (out or "ssh failed")[-400:]
                    )
                    log.warning(
                        "Series ping failed for %s: %s", name, (out or "")[-200:]
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the series alive
                log.exception("Series iteration failed")
                delay = 30
            await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Daily auto start/stop schedule
    # ------------------------------------------------------------------

    async def _sched_loop(self) -> None:
        tz = ZoneInfo(self.settings.schedule_tz)
        while True:
            try:
                now = datetime.now(tz)
                hhmm = now.strftime("%H:%M")
                today = now.strftime("%Y-%m-%d")
                for cs in await self.db.list_scheduled():
                    key = str(cs["_id"])
                    account = await self.db.get_account(cs["account_id"])
                    if not account:
                        continue
                    if (
                        cs.get("schedule_stop") == hhmm
                        and cs.get("sched_last_stop") != today
                    ):
                        await self.db.update_codespace_fields(
                            key, {"sched_last_stop": today}
                        )
                        await self._scheduled_stop(cs, account)
                    if (
                        cs.get("schedule_start") == hhmm
                        and cs.get("sched_last_start") != today
                    ):
                        await self.db.update_codespace_fields(
                            key, {"sched_last_start": today}
                        )
                        await self._scheduled_start(cs, account)
                # Series-wide schedule (applies to the series as a whole).
                series = await self.db.get_series()
                if (
                    series.get("schedule_stop") == hhmm
                    and series.get("sched_last_stop") != today
                ):
                    await self.db.save_series({"sched_last_stop": today})
                    if series.get("running") or self.series_running():
                        await self.stop_series()
                        await self.db.save_series({"resume": True})
                        log.info("Series stopped by the series schedule")
                if (
                    series.get("schedule_start") == hhmm
                    and series.get("sched_last_start") != today
                ):
                    await self.db.save_series({"sched_last_start": today})
                    if series.get("cs_ids") and not self.series_running():
                        await self.start_series()
                        log.info("Series started by the series schedule")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the scheduler alive
                log.exception("Scheduler iteration failed")
            await asyncio.sleep(20)

    async def _scheduled_stop(self, cs: dict, account: dict) -> None:
        key = str(cs["_id"])
        name = cs["name"]
        # Schedules apply to series codespaces too: pause the whole series
        # so it doesn't immediately re-boot the codespace we are stopping.
        series = await self.db.get_series()
        if key in [str(x) for x in (series.get("cs_ids") or [])] and (
            series.get("running") or self.series_running()
        ):
            # stop_series also shuts the active codespace down.
            await self.stop_series()
            await self.db.save_series({"resume": True})
            await self.db.record_ping(key, True, "scheduled stop (series paused)")
            log.info("Series paused by scheduled stop of %s", name)
            return
        # Remember whether keep-alive was on so the scheduled start can
        # resume it. Then pause it, otherwise it would re-boot the codespace.
        resume = bool(cs.get("keepalive")) or self.is_running(key)
        if self.is_running(key):
            await self.stop(key)
        await self.db.update_codespace_fields(key, {"keepalive_resume": resume})
        try:
            await self.gh.stop_codespace(account, name)
            await self.db.set_state(key, "Shutdown")
            await self.db.set_startup_done(key, False)
            await self.db.record_ping(key, True, "scheduled stop")
            log.info("Scheduled stop done for %s", name)
        except Exception as exc:  # noqa: BLE001
            await self.db.record_ping(
                key, False, f"scheduled stop failed: {exc}"[:400]
            )
            log.exception("Scheduled stop failed for %s", name)

    async def _scheduled_start(self, cs: dict, account: dict) -> None:
        key = str(cs["_id"])
        name = cs["name"]
        # Schedules apply to series codespaces too: resume the series with
        # this codespace as the active one; the series loop boots it, runs
        # the startup commands and keeps rotating on rate limits.
        series = await self.db.get_series()
        if key in [str(x) for x in (series.get("cs_ids") or [])] and (
            series.get("resume") or series.get("running")
        ):
            await self.start_series(active_cs_id=key)
            await self.db.record_ping(key, True, "scheduled start (series)")
            log.info("Series resumed by scheduled start of %s", name)
            return
        try:
            # SSH connect boots a stopped codespace.
            rc, out = await self.gh.ssh_exec(account, name, PING_COMMAND)
            if rc != 0:
                raise RuntimeError((out or "ssh failed")[-300:])
            await self.db.record_ping(key, True, "scheduled start")
            await self.db.set_state(key, "Available")
            await self.run_startup_commands(key, reason="scheduled start", force=True)
            log.info("Scheduled start done for %s", name)
        except Exception as exc:  # noqa: BLE001
            await self.db.record_ping(
                key, False, f"scheduled start failed: {exc}"[:400]
            )
            log.exception("Scheduled start failed for %s", name)
        if cs.get("keepalive_resume"):
            await self.db.update_codespace_fields(key, {"keepalive_resume": False})
            await self.start(key)

    # ------------------------------------------------------------------
    # Startup commands (shared by keep-alive loop and watcher)
    # ------------------------------------------------------------------

    async def run_startup_commands(self, cs_id, *, reason: str, force: bool = False) -> None:
        """Run the codespace's startup commands once per 'up' session.

        The `startup_done` field acts as the session marker: once the
        commands ran, it is persisted as True; any caller that arrives later
        for the same session sees that and skips. Works across bot restarts
        because the marker lives in MongoDB.
        """
        key = str(cs_id)
        lock = self._startup_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cs = await self.db.get_codespace(key)
            if not cs:
                return
            if not force and cs.get("startup_done"):
                return  # already handled for this session
            account = await self.db.get_account(cs["account_id"])
            if not account:
                return
            cmds = cs.get("startup_commands") or []
            if not cmds:
                await self.db.set_startup_done(key, True)
                return
            name = cs["name"]
            workdir = (cs.get("startup_dir") or "").strip()
            log.info(
                "Running %d startup command(s) on %s (%s)%s",
                len(cmds),
                name,
                reason,
                f" in {workdir}" if workdir else " (auto workspace)",
            )
            ok = 0
            for cmd in cmds:
                full_cmd = build_startup_command(cmd, workdir=workdir if workdir else None)
                rc, out = await self.gh.ssh_exec(account, name, full_cmd, timeout=600)
                if rc == 0:
                    ok += 1
                else:
                    log.warning(
                        "Startup command failed on %s: %s -> %s",
                        name,
                        cmd,
                        (out or "")[-200:],
                    )
            await self.db.set_startup_done(key, True)
            await self.db.set_state(key, "Available")
            await self.db.record_ping(
                key,
                ok == len(cmds),
                f"startup commands ({reason}): {ok}/{len(cmds)} succeeded",
            )

    # ------------------------------------------------------------------
    # Watcher: detect starts from ANYWHERE (bot, web, VS Code, gh CLI)
    # ------------------------------------------------------------------

    async def _watch_loop(self) -> None:
        interval = self.settings.watch_interval
        while True:
            try:
                for account in await self.db.all_accounts():
                    for cs in await self.db.list_codespaces(account["_id"]):
                        if not cs.get("startup_commands"):
                            continue
                        key = str(cs["_id"])
                        prev = cs.get("state")
                        try:
                            state = await self.gh.get_codespace_state(
                                account, cs["name"]
                            )
                        except Exception:  # noqa: BLE001
                            continue
                        if state == "Available":
                            if not cs.get("startup_done") or prev != "Available":
                                log.info(
                                    "Detected start of %s (was: %s, startup_done: %s)",
                                    cs["name"],
                                    prev or "unknown",
                                    cs.get("startup_done"),
                                )
                                await self.db.set_state(key, "Available")
                                await self.run_startup_commands(
                                    key, reason="start detected"
                                )
                            elif state != prev:
                                await self.db.set_state(key, state)
                        else:
                            if state != prev:
                                await self.db.set_state(key, state)
                            if state in ("Shutdown", "Stopped"):
                                await self.db.set_startup_done(key, False)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the watcher alive
                log.exception("Watcher iteration failed")
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------
    # Keep-alive loop
    # ------------------------------------------------------------------

    async def _loop(self, cs_id: str) -> None:
        interval = self.settings.keepalive_interval
        failures = 0
        while True:
            try:
                cs = await self.db.get_codespace(cs_id)
                if not cs or not cs.get("keepalive"):
                    return
                account = await self.db.get_account(cs["account_id"])
                if not account:
                    await self.db.record_ping(cs_id, False, "account removed")
                    return
                name = cs["name"]

                prev = cs.get("state")
                try:
                    state = await self.gh.get_codespace_state(account, name)
                    if state != prev:
                        await self.db.set_state(cs_id, state)
                        if state in ("Shutdown", "Stopped"):
                            await self.db.set_startup_done(cs_id, False)
                except Exception:  # noqa: BLE001
                    state = "Unknown"

                # If codespace is Shutdown or Stopped, boot it via GitHub REST API
                if state in ("Shutdown", "Stopped"):
                    try:
                        log.info("Codespace %s is %s. Booting via GitHub API...", name, state)
                        await self.gh.start_codespace(account, name)
                    except Exception as boot_err:
                        log.warning("Could not boot %s via API: %s", name, boot_err)

                rc, out = await self.gh.ssh_exec(account, name, PING_COMMAND)
                if rc == 0:
                    failures = 0
                    await self.db.record_ping(cs_id, True, "ping ok")
                    refreshed = await self.db.get_codespace(cs_id)
                    if refreshed and (not refreshed.get("startup_done") or state != "Available" or prev != "Available"):
                        # Make sure startup commands run. The shared lock prevents double-runs.
                        await self.run_startup_commands(
                            cs_id, reason="keep-alive connect"
                        )
                else:
                    api_ok = False
                    try:
                        await self.gh.start_codespace(account, name)
                        new_state = await self.gh.get_codespace_state(account, name)
                        if new_state in ("Available", "Starting"):
                            api_ok = True
                            state = new_state
                    except Exception as boot_err:
                        out = f"SSH: {out} | API: {boot_err}"

                    if api_ok:
                        failures = 0
                        await self.db.record_ping(cs_id, True, "ping ok (API start)")
                        refreshed = await self.db.get_codespace(cs_id)
                        if refreshed and not refreshed.get("startup_done"):
                            await self.run_startup_commands(
                                cs_id, reason="api start"
                            )
                    else:
                        failures += 1
                        await self.db.record_ping(cs_id, False, (out or "ssh failed")[-400:])
                        log.warning("Keep-alive ping failed for %s: %s", name, (out or "")[-200:])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                failures += 1
                log.exception("Keep-alive iteration failed for %s", cs_id)
                try:
                    await self.db.record_ping(cs_id, False, str(exc)[:400])
                except Exception:  # noqa: BLE001
                    pass

            # Back off a little on repeated failures, otherwise ping every
            # `interval` seconds (default 300s = 5 minutes).
            delay = interval if failures == 0 else min(interval, 60 * min(failures, 5))
            await asyncio.sleep(delay)
