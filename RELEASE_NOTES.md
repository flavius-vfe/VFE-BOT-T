# VFE Docker Bot v0.4.0

## Added

- Live container filters: all, running, stopped, and unhealthy
- Rich container cards with uptime, health, CPU, RAM, restart count, exit code, and OOM state
- Automatic Docker Compose project discovery
- Confirmed stack start, stop, and restart actions that skip protected containers
- Stack ZIP and all-container ZIP exports
- Sanitized diagnostics bundle
- Container created/removed, health, restart-loop, OOM, and Docker connectivity alerts
- `/settings`, `/diagnostics`, and confirmed `/unpair`

## Improved

- Standalone containers are isolated into individual groups
- Schedule duplicates are prevented
- Failed schedules retry with configurable cooldown and maximum attempts
- Schedules for removed containers are disabled automatically
- Stack actions report partial failures
- Compose-generated runtime labels are removed from exports
- Version is loaded from the `VERSION` file and included in the Docker image

## Upgrade

```bash
cd /mnt/user/appdata/vfe-bot-t
bash update.sh
```

The SQLite schema is migrated automatically. Existing pairing, audit history, approvals, and schedules remain in the database.
