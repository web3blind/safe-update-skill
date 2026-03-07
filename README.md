# safe-update

Stateful and low-risk OpenClaw update workflow with precheck, backup, release-impact analysis, postcheck, auto-rollback, and cleanup.

## Features

- **Disk space check** — fails if less than 500MB free (configurable)
- **Auto-rollback** — automatically restores from backup if postcheck fails
- **Backup** — archives config, memory, workspace files to timestamped tar.gz
- **Postcheck** — verifies gateway and heartbeat state after update
- **State persistence** — resumes from saved state after restart

## Requirements

- Python 3.8+
- OpenClaw installed

## Installation

```bash
cd ~/.openclaw/workspace/skills
git clone git@github.com:your-repo/safe-update.git
```

## Usage

```bash
# Dry-run (preview actions, no update)
python3 ~/.openclaw/workspace/skills/safe-update/scripts/safe_update.py dry-run

# Full update with safety checks
python3 ~/.openclaw/workspace/skills/safe-update/scripts/safe_update.py run

# Resume after restart
python3 ~/.openclaw/workspace/skills/safe-update/scripts/safe_update.py resume

# Cleanup old backups
python3 ~/.openclaw/workspace/skills/safe-update/scripts/safe_update.py cleanup
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SAFE_UPDATE_KEEP_LAST_SUCCESS` | 3 | Number of backups to keep |
| `SAFE_UPDATE_MAX_AGE_DAYS` | 14 | Max age for backups in days |
| `SAFE_UPDATE_UPDATE_CMD` | — | Update command to run |
| `SAFE_UPDATE_MIN_FREE_MB` | 500 | Minimum free disk space in MB |
| `SAFE_UPDATE_AUTO_ROLLBACK` | 1 | Enable auto-rollback on postcheck failure |

## Example

```bash
# Set update command
export SAFE_UPDATE_UPDATE_CMD="openclaw update --yes --no-restart"

# Run dry-run to preview
python3 ~/.openclaw/workspace/skills/safe-update/scripts/safe_update.py dry-run

# Run actual update
python3 ~/.openclaw/workspace/skills/safe-update/scripts/safe_update.py run
```

## License

MIT
