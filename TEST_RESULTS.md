# VFE Docker Bot v0.4.0 validation

Validation date: 2026-08-07

## Passed

- `python -m py_compile vfe_bot/*.py`
- `PYTHONPATH=. pytest -q`
- **15 automated tests passed**
- Existing v0.3.1 command/menu compatibility tests
- Live-state button rendering tests
- Container filtering tests
- Atomic approval tests
- Duplicate-safe schedule tests
- Failed-schedule tracking tests
- Removed-container schedule cleanup test
- Legacy SQLite schedule-schema migration test
- Automatic container discovery and protected-container tests
- Compose project grouping and bulk action tests
- YAML and Unraid XML export tests
- Stack and all-container ZIP export tests
- Secret-redaction tests
- VERSION-file reporting test
- `bash -n install.sh update.sh`
- YAML parsing for `docker-compose.yml` and the GitHub Actions workflow

## Not performed in this environment

- A real Docker image build, because the Docker CLI/daemon is unavailable here
- A live Unraid deployment
- Live Telegram Bot API authentication and button interaction
- Restore testing of exported profiles against an actual Unraid server

These items should be smoke-tested on a non-critical container or test stack before broad production use.
