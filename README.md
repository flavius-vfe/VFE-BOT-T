# VFE Docker Bot for Unraid

A lightweight, button-first Telegram controller that automatically discovers Docker containers on an Unraid server. It does not run an LLM and is designed for small systems.

## v0.4.1 maintenance release

This release keeps the v0.4 container, stack, monitoring, scheduling, export, and diagnostics features while tightening repository hygiene and license compliance:

- Adds `uninstall.sh` with safe keep-data and permanent purge modes
- Adds `.dockerignore` so `.env`, databases, logs, tests, and runtime data never enter the Docker build context
- Includes direct dependency license texts and third-party notices
- Generates a complete license bundle for every Python dependency installed in the Docker image
- Fails the Docker build if an installed dependency has no recognizable license or notice file
- Removes generated validation reports and release-only notes from the repository package

## v0.4 features

### Rich container status

Container pages now show:

- Running, stopped, paused, restarting, created, dead, and removing states
- Docker health-check result
- Uptime
- CPU and memory usage when running
- Restart count
- Exit code and out-of-memory warnings
- Compose project, image, ports, network, mounts, and restart policy

The container list has live filters for **All**, **Running**, **Stopped**, and **Unhealthy**.

### Compose stack management

The bot reads Docker Compose labels automatically and groups containers into projects. Open **Stacks** to:

- View every container and state in a project
- Start stopped containers in the project
- Stop running containers in the project
- Restart running containers in the project
- Export the complete project as a ZIP

Containers without Compose labels are kept in separate single-container `Standalone: NAME` groups. They are deliberately not combined into one dangerous bulk-action group.

Stack actions require confirmation and skip protected containers.

### Monitoring and alerts

Optional Telegram alerts cover:

- Container state changes
- Healthy/unhealthy transitions
- Container creation and removal
- Possible restart loops
- Out-of-memory kills
- Docker daemon connection failure and recovery

The monitor also disables restart schedules automatically when their container has been removed.

### Export and backup

Exports are available for:

- One container as Compose YAML
- One container as Unraid XML
- One Compose stack as a ZIP
- Every container as a ZIP

Stack/all ZIP files contain:

```text
stack-name.compose.yaml
manifest.json
unraid/my-container.xml
```

Environment variables, labels, command arguments, and URLs are sanitized when they appear to contain passwords, tokens, API keys, credentials, claims, cookies, sessions, or embedded URL usernames/passwords. Always review an export before restoring it.

### Safer schedules

Daily restart schedules are now:

- Duplicate-safe for the same container and time
- Retried after a configurable delay when a restart fails
- Limited to a configurable maximum number of attempts
- Automatically disabled when a container disappears
- Recorded in the audit log with failure details

### Diagnostics and account controls

- `/diagnostics` downloads a sanitized ZIP containing server/container metadata, schedules, and recent audit entries.
- Telegram tokens and pairing codes are never included.
- `/settings` shows active runtime settings.
- `/unpair` removes the current Telegram owner after confirmation.
- The displayed app version is read from the repository `VERSION` file.

## Simple installation

Only two values are required:

- Telegram bot token from `@BotFather`
- A private pairing code

After the GitHub repository is public:

```bash
curl -fsSL https://raw.githubusercontent.com/flavius-vfe/VFE-BOT-T/main/install.sh | bash
```

The default installation directory is:

```text
/mnt/user/appdata/vfe-bot-t
```

Then open a private Telegram chat with the bot and send:

```text
/pair YOUR_PAIRING_CODE
```

## Manual installation

```bash
cd /mnt/user/appdata
git clone https://github.com/flavius-vfe/VFE-BOT-T.git vfe-bot-t
cd vfe-bot-t
bash install.sh
```

## Main commands

Container names remain optional.

```text
/containers     live container list and filters
/stacks         Compose project list and bulk actions
/server         Docker, CPU, RAM, and storage summary
/startc         select a stopped container
/stop           select a running container
/restart        select a running container
/logs           select a container and log length
/stats          select a running container
/schedule       select a container and restart time
/export         one-container, stack, or all-profile exports
/schedules      view and disable restart schedules
/settings       current bot settings
/diagnostics    sanitized support ZIP
/audit          recent actions
/unpair         remove the paired Telegram owner
/help           main menu
```

Typed commands such as `/restart plex` still work.

## Updating

```bash
cd /mnt/user/appdata/vfe-bot-t
bash update.sh
```

Or manually:

```bash
cd /mnt/user/appdata/vfe-bot-t
git pull --ff-only
docker compose up -d --build --remove-orphans
```

The existing database is migrated automatically at startup.

## Uninstalling

A normal uninstall removes the running container and Compose network but keeps `.env`, the SQLite database, schedules, audit history, and repository files so you can reinstall easily:

```bash
cd /mnt/user/appdata/vfe-bot-t
bash uninstall.sh
```

Non-interactive equivalent:

```bash
cd /mnt/user/appdata/vfe-bot-t
bash uninstall.sh --yes
```

To remove the container and its Docker image while still keeping configuration and data:

```bash
bash uninstall.sh --remove-image
```

To permanently delete the complete installation, including the Telegram token, pairing configuration, database, schedules, audit history, and repository directory:

```bash
cd /mnt/user/appdata/vfe-bot-t
bash uninstall.sh --purge
```

Non-interactive permanent removal:

```bash
bash /mnt/user/appdata/vfe-bot-t/uninstall.sh --purge --yes
```

`--purge` cannot be undone. The script refuses known unsafe paths and verifies that the target looks like a VFE-BOT-T installation before deleting it.

You can also run the non-purge uninstaller directly from GitHub:

```bash
curl -fsSL https://raw.githubusercontent.com/flavius-vfe/VFE-BOT-T/main/uninstall.sh | bash -s -- --yes
```

## Configuration

The installer creates `.env`. Useful settings:

```dotenv
TZ=Europe/Bucharest
BOT_CONTAINER_NAME=vfe-bot-t
PROTECTED_CONTAINERS=swag,cloudflared,mariadb

NOTIFY_CONTAINER_CHANGES=true
NOTIFY_HEALTH_CHANGES=true
NOTIFY_CREATED_REMOVED=true
POLL_INTERVAL_SECONDS=30
RESTART_LOOP_THRESHOLD=3

SCHEDULE_RETRY_MINUTES=5
SCHEDULE_MAX_ATTEMPTS=3

LOG_LINES_DEFAULT=50
LOG_LINES_MAX=300
DIAGNOSTICS_AUDIT_LIMIT=50
```

The bot always adds its own container name to the protected set.

## Resource use

The Compose configuration limits the bot to 192 MB RAM and 80 processes. Actual use depends on the number of containers and Telegram activity. Collecting live stats is performed when opening a container or stats screen, not continuously for every container.

## Security warning

For simple installation, this project mounts:

```text
/var/run/docker.sock
```

Docker socket access is effectively administrator-level access to the Docker host. Keep the Telegram token and pairing code secret, pair only in a private chat, enable Telegram two-step verification, and protect critical containers using `PROTECTED_CONTAINERS`.

The bot intentionally does not expose arbitrary shell execution, container creation, container deletion, or free-form Docker API calls.

The `.dockerignore` file excludes `.env`, `data/`, databases, logs, tests, and Git history from the Docker build context. The Dockerfile also uses explicit `COPY` instructions and never copies the complete repository into the image.

## Licenses

VFE-BOT-T is licensed under the MIT License in `LICENSE`.

Direct dependency license texts are stored under `licenses/third-party/`, and attribution details are summarized in `THIRD_PARTY_NOTICES.md`. During each image build, pip records exactly which direct and transitive dependencies were installed. Their packaged license and notice files are copied into:

```text
/usr/share/licenses/vfe-bot-t/
```

The image build stops with an error if that generated dependency license bundle is incomplete. Python and Debian license files supplied by the official `python:3.12-slim` base image remain in their standard locations.

## Development and testing

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
PYTHONPATH=. pytest -q
```

The Docker image copies `VERSION` into the runtime so the displayed version matches the release package.
