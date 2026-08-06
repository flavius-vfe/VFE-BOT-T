# VFE Docker Bot for Unraid

A lightweight, button-first Telegram bot that automatically discovers and manages Docker containers on an Unraid server.

## What is new in v0.3.0

You no longer need to type Docker container names.

1. Open **Containers** or select an action such as **Restart**.
2. Tap a container from the automatically generated list.
3. Tap the action you need.
4. Confirm actions that change container state.

Each container screen includes:

- Start, stop and restart
- Logs with selectable line counts
- CPU, RAM and network statistics
- Daily restart scheduling
- Compose YAML export
- Unraid XML template export
- Refresh and navigation buttons

Action lists are filtered automatically. For example, **Start** displays stopped containers, while **Stop**, **Restart**, and **Stats** display running containers.

## Simple setup

Only two values are required:

- Telegram bot token from `@BotFather`
- A private pairing code chosen by you

The bot discovers all current and future Docker containers automatically. There is no container allowlist to maintain.

## One-command install on Unraid

After this repository is public:

```bash
curl -fsSL https://raw.githubusercontent.com/flavius-vfe/VFE-BOT-T/main/install.sh | bash
```

The installer clones the repository into `/mnt/user/appdata/vfe-bot-t`, asks for the token and pairing code, builds the image, and starts the bot.

Then open the Telegram bot in a private chat and send:

```text
/pair YOUR_PAIRING_CODE
```

## Manual install

```bash
cd /mnt/user/appdata
git clone https://github.com/flavius-vfe/VFE-BOT-T.git vfe-bot-t
cd vfe-bot-t
bash install.sh
```

## Button-first commands

These commands open menus; container names are optional:

```text
/containers     select and manage any container
/server         Docker, CPU, RAM and storage summary
/startc         select a stopped container to start
/stop           select a running container to stop
/restart        select a running container to restart
/logs           select a container and log length
/stats          select a running container
/schedule       select a container and restart time
/export         select a container and YAML/XML format
/schedules      view and disable schedules using buttons
/audit          recent actions
/help           main menu
```

Typed commands such as `/restart plex` still work, but they are no longer required.

## Container profile export

Open a container, tap **Export**, then choose:

- **Compose YAML** — a Docker Compose-style service profile
- **Unraid XML** — an Unraid Docker template profile

The export includes the image, environment variables, ports, paths, restart policy, network mode, command, labels and other relevant container settings.

For safety, environment values whose names contain terms such as `TOKEN`, `PASSWORD`, `SECRET`, `API_KEY`, `AUTH`, or `CLAIM` are exported as:

```text
<redacted>
```

Replace those placeholders manually before using an exported profile.

## Automatic discovery

The bot queries Docker whenever a container menu is opened. New containers appear automatically after they are created in Unraid. Removed containers disappear automatically.

The bot protects its own `vfe-bot-t` container from start, stop, restart and scheduling operations. Add other protected names in `.env`:

```dotenv
PROTECTED_CONTAINERS=swag,cloudflared,mariadb
```

Read-only actions such as status, logs, statistics and export remain available for protected containers.

## Updating an existing installation

```bash
cd /mnt/user/appdata/vfe-bot-t
bash update.sh
```

To force a clean image rebuild after this release:

```bash
cd /mnt/user/appdata/vfe-bot-t
git pull
docker compose build --no-cache
docker compose up -d
```

Telegram command menus are refreshed automatically when the new container starts.

## Configuration

The installer creates `.env`. Common options:

```dotenv
TZ=Europe/Bucharest
NOTIFY_CONTAINER_CHANGES=true
POLL_INTERVAL_SECONDS=30
LOG_LINES_DEFAULT=50
LOG_LINES_MAX=300
PROTECTED_CONTAINERS=
```

To pair with a different Telegram account, stop the bot and remove `data/vfe-bot.db`, then start it and pair again.

## Resource use

The Compose file limits the bot to 192 MB of RAM and 80 processes. It does not run an LLM.

## Security warning

For simple installation this version mounts `/var/run/docker.sock`. Access to that socket is highly privileged. Keep the Telegram token and pairing code secret, pair only in a private chat, and protect the Telegram account with two-step verification.

The bot intentionally does not offer arbitrary shell execution, container deletion or container creation.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
PYTHONPATH=. pytest -q
```
