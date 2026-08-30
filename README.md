# Codespace Keeper Bot

A Python Telegram bot that keeps your **GitHub Codespaces alive** by
connecting to them over SSH (via the GitHub CLI) and sending a tiny request
every 5 minutes, so GitHub's idle timeout never kicks in.

## Features

- **Keep-alive over SSH** — connects with `gh codespace ssh` and pings every
  5 minutes (configurable). Connecting also auto-starts a stopped codespace.
- **Login from the bot** — device login (same flow as `gh auth login`) or
  token paste / `/add_account` (the message is deleted immediately).
- **Start / Stop** — toggle keep-alive per codespace via buttons or commands.
  Stopping doesn't just pause the pings: the bot also runs
  `gh codespace stop`, so the codespace itself shuts down.
- **Multiple GitHub accounts** — store any number of accounts, each with its
  own isolated `gh` config and SSH keys (persisted in MongoDB).
- **Multiple codespaces** — browse and manage all codespaces per account.
- **Startup commands run on EVERY start** — per-codespace shell commands run
  automatically every time the codespace starts, no matter when or where it
  was started (this bot, github.com, VS Code, gh CLI). A background watcher
  checks all tracked codespaces every `WATCH_INTERVAL` seconds (default 60)
  and fires the commands the moment a codespace comes up. Configured in two
  steps: 1) the directory to `cd` into (like `~` or `~/mydirectory`), 2) the
  sh command(s) that run there (`cd <dir> && <cmd>`).
- **Auto start/stop schedule (IST)** — set daily times per codespace: the
  bot shuts the codespace down at your stop time (pausing keep-alive) and
  boots it again at your start time (running startup commands and resuming
  keep-alive). Timezone configurable via `SCHEDULE_TZ` (default
  `Asia/Kolkata`).
- **Series: rate-limit failover** — pick any codespaces (across accounts)
  and run them one at a time, in order. The moment the active one replies
  with a GitHub rate-limit error ("API rate limit exceeded", "secondary
  rate limit", HTTP 429, ...), the bot stops it and starts the next in the
  series — looping back to the first forever. Auto start/stop schedules
  apply to series codespaces too, and the series can also have its OWN
  daily stop/start schedule (set from the series menu or
  `/series_schedule`).
- **MongoDB cluster storage** — ALL state (accounts, tokens, SSH keys,
  codespaces, startup commands, keep-alive flags) lives in your external
  MongoDB cluster (e.g. MongoDB Atlas). If your server restarts or dies,
  just deploy the bot on another machine with the same `.env` — it resumes
  exactly where it left off, keep-alives included.
- **Full Docker Compose** — one command brings up the bot (GitHub CLI
  auto-installed in the image).

## Quick start

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Create a MongoDB cluster (free tier works) at
   [mongodb.com/atlas](https://www.mongodb.com/atlas):
   - Create a database user (Database Access).
   - Allow your server's IP in **Network Access** (or `0.0.0.0/0` if your
     server IP changes).
   - Copy the connection string: *Database → Connect → Drivers*.
3. Configure the environment:

   ```bash
   cp .env.example .env
   # edit .env: set BOT_TOKEN, OWNER_IDS and MONGO_URI, e.g.
   # MONGO_URI=mongodb+srv://user:pass@cluster0.abcde.mongodb.net/?retryWrites=true&w=majority
   ```

4. Build and run:

   ```bash
   docker compose up -d --build
   ```

5. Open your bot in Telegram and send `/start` (buttons) or `/help` (commands).

### Moving to a new server

Everything is in MongoDB, so recovery is trivial:

```bash
# on the new server
unzip codespace-keeper.zip && cd codespace-keeper
# copy your old .env (same MONGO_URI!)
docker compose up -d --build
```

Accounts, SSH keys, tracked codespaces, startup commands and running
keep-alives are all restored automatically on startup.

### Optional: local MongoDB instead

If you ever want a local MongoDB container instead of a cluster:

```bash
docker compose -f docker-compose.yml -f docker-compose.local-mongo.yml up -d --build
```

## Bot commands

Everything can be done with buttons (`/start`) **or** slash commands.
Accounts are addressed by their *alias*; accounts added via the button flow
can be addressed by their GitHub login.

**Accounts**

| Command | Description |
| --- | --- |
| `/add_account <alias> <PAT>` | Add GitHub account (your message auto-deletes) |
| `/remove_account <alias>` | Remove account + all its codespaces |
| `/accounts` | List all accounts |

**Codespaces**

| Command | Description |
| --- | --- |
| `/list <alias>` | Remote + tracked codespaces |
| `/add_codespace <alias> <name>` | Track a codespace |
| `/remove_codespace <alias> <name>` | Untrack (stops its keep-alive) |

**Startup commands**

| Command | Description |
| --- | --- |
| `/set_startup <alias> <name> <cmd1;cmd2>` | Sh command(s) that auto-run every time the codespace starts, `;`-separated |
| `/run_startup <alias> <name>` | Manually run startup command(s) immediately |
| `/set_dir <alias> <name> <dir>` | Directory to `cd` into before the commands (e.g. `~/mydirectory`) |
| `/clear_startup <alias> <name>` | Clear directory + commands |

**Keep-alive**

| Command | Description |
| --- | --- |
| `/keep <alias> <name>` | Start keep-alive (auto-tracks if needed) |
| `/stop <alias> <name>` | Stop keep-alive **and shut the codespace down** |
| `/keep_all` | Start all tracked codespaces |
| `/stop_all` | Stop every keep-alive and shut those codespaces down |

**Auto start/stop (daily, IST)**

| Command | Description |
| --- | --- |
| `/schedule <alias> <name> <stop> <start>` | Daily stop/start times, 24h `HH:MM` (use `-` to skip one) |
| `/unschedule <alias> <name>` | Remove the schedule |

**Series (rate-limit failover)**

| Command | Description |
| --- | --- |
| `/series` | Show the series, order and active codespace |
| `/series_add <alias> <name>` | Add a codespace (order = add order) |
| `/series_remove <alias> <name>` | Remove a codespace |
| `/series_start` | Run the series (one codespace at a time) |
| `/series_stop` | Stop the series (shuts the active codespace down) |
| `/series_schedule <stop> <start>` | Daily stop/start for the whole series, 24h `HH:MM` IST (use `-` to skip one) |
| `/series_unschedule` | Remove the series schedule |
| `/series_clear` | Empty the series |

**Info**

| Command | Description |
| --- | --- |
| `/status` | Full dashboard (accounts, codespaces, last pings) |
| `/help` | Command list |

## Configuration

| Variable             | Default                  | Description                              |
| -------------------- | ------------------------ | ---------------------------------------- |
| `BOT_TOKEN`          | — (required)             | Telegram bot token                       |
| `OWNER_IDS`          | empty (⚠️ open to all)   | Allowed Telegram user IDs                |
| `MONGO_URI`          | `mongodb://mongo:27017`  | MongoDB **cluster** connection string    |
| `MONGO_DB`           | `codespace_keeper`       | Database name                            |
| `KEEPALIVE_INTERVAL` | `300`                    | Seconds between SSH pings                |
| `WATCH_INTERVAL`     | `60`                     | Seconds between start-detection checks   |
| `SCHEDULE_TZ`        | `Asia/Kolkata`           | Timezone for schedules + ALL shown times |
| `DATA_DIR`           | `/app/data`              | Per-account gh config / SSH key cache    |

## How it works

- Each GitHub account gets its own `HOME` + `GH_CONFIG_DIR` under `DATA_DIR`,
  and its token is injected via `GH_TOKEN` — so any number of accounts work
  side by side. These on-disk files are only a cache; the source of truth is
  MongoDB, and keys are re-materialized from the cluster whenever missing.
- The first `gh codespace ssh` generates an SSH keypair; the bot stores it in
  MongoDB and rewrites it to disk whenever needed (e.g. on a fresh server).
- A background asyncio task per codespace runs
  `gh codespace ssh -c <name> -- echo ping` every `KEEPALIVE_INTERVAL`
  seconds. If the codespace was stopped, the SSH connect boots it and the
  startup commands are executed again.
- A single watcher task polls the state of every tracked codespace with
  startup commands (every `WATCH_INTERVAL` seconds). When it sees a
  codespace transition to `Available` — even if it was started from
  github.com, VS Code or the `gh` CLI — it connects over SSH and runs the
  startup commands. The last-seen state is persisted in MongoDB, so the
  commands run exactly once per boot (no duplicates when the keep-alive
  loop and the watcher overlap, and no re-runs after a bot restart).
- A scheduler task checks every 20 seconds whether a codespace's daily stop
  or start time (in `SCHEDULE_TZ`, default IST) has been reached. At stop
  time it runs `gh codespace stop` and pauses keep-alive; at start time it
  boots the codespace over SSH, runs the startup commands and resumes
  keep-alive if it was on before. Every timestamp the bot shows (last
  ping, dashboard) is rendered in `SCHEDULE_TZ` too — it does not matter
  which country your server runs in, times are always IST by default
  (docker-compose also sets the container clock `TZ` accordingly).
- The series loop pings only the **active** codespace of the series (every
  `KEEPALIVE_INTERVAL` seconds). If the ping output contains a rate-limit
  message (`rate limit`, `too many requests`, `429`, ...), the active
  codespace is stopped and the next one in the series is booted (startup
  commands included) — rotating in a circle. If a series codespace has an
  auto start/stop schedule, the stop time pauses the whole series and the
  start time resumes it with that codespace as active. The series can also
  carry its own schedule: at its stop time the rotation pauses and the
  active codespace is shut down; at its start time the rotation resumes
  from where it left off. The series state lives in MongoDB, so it
  survives bot restarts.

## Security notes

- **Set `OWNER_IDS`.** Without it, anyone who discovers your bot can control
  your codespaces.
- GitHub tokens and SSH private keys are stored **in plain text** in MongoDB.
  Protect the cluster: strong database user password, TLS (Atlas default),
  and restrict Network Access to your server IPs where possible.
- Prefer fine-grained/limited-scope tokens where possible.

## Notes & limits

- Keeping codespaces alive 24/7 consumes your GitHub Codespaces
  usage-hours quota — watch your billing.
- Startup commands should exit; long-running processes must be backgrounded
  (`nohup ... &`), otherwise the command times out after 10 minutes.
