# VFE Docker Bot for Unraid

A lightweight Telegram bot that automatically discovers and manages Docker containers on an Unraid server.

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

## Commands

```text
/containers                  automatically list every container
/server                      Docker, CPU, RAM and array storage summary
/info plex                   container details
/logs plex 100               recent logs
/stats plex                  CPU, RAM and network usage
/startc plex                 request start confirmation
/stop plex                   request stop confirmation
/restart plex                request restart confirmation
/schedule plex 04:30         restart Plex every day at 04:30
/schedules                   list schedules
/unschedule a1b2c3           disable a schedule
/audit 20                    recent actions
```

Container lists include inline buttons. Start, stop and restart always require a second confirmation click.

Plain text works too:

```text
restart plex
logs sonarr 100
server
```

## Automatic discovery

The bot queries Docker each time `/containers` is opened. New containers appear automatically after they are created in Unraid. Removed containers disappear automatically.

The bot protects its own `vfe-bot-t` container from start, stop and restart operations. Add other protected names in `.env`:

```dotenv
PROTECTED_CONTAINERS=swag,cloudflared,mariadb
```

Read-only actions such as status, logs and stats remain available for protected containers.

## Updating

```bash
cd /mnt/user/appdata/vfe-bot-t
bash update.sh
```

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

The bot exposes only list, info, logs, stats, start, stop and restart features, but a compromise of the bot process could still expose the Docker daemon. A future hardened deployment can use a separate Docker API proxy.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```
