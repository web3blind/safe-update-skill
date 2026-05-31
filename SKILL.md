---
name: safe-update
description: Stateful and low-risk Hermes update workflow with precheck, backup, release-impact analysis, postcheck, auto-rollback, and cleanup. Use when user asks to safely update Hermes with rollback-ready backups and minimal manual steps.
---

# safe-update

Use this skill for Hermes upgrades when reliability matters.

## Workflow (stateful)
1. `dry-run` — precheck + disk space check + backup + release-impact + plan (no update).
2. `run` — full pipeline: precheck → disk space check → backup → update → postcheck → auto-rollback (if needed) → report.
3. `resume` — continue from saved state after restart.
4. `cleanup` — remove old backups using retention policy.

## Commands
- `python3 ~/.hermes/skills/safe-update/scripts/safe_update.py dry-run`
- `python3 ~/.hermes/skills/safe-update/scripts/safe_update.py run`
- `python3 ~/.hermes/skills/safe_update.py resume`
- `python3 ~/.hermes/skills/safe-update/scripts/safe_update.py cleanup`

## Features
- **Disk space check** — fails if less than 500MB free (configurable via `SAFE_UPDATE_MIN_FREE_MB`)
- **Auto-rollback** — automatically restores from backup if postcheck fails (configurable via `SAFE_UPDATE_AUTO_ROLLBACK`)
- **Backup** — archives config, memory, workspace files to timestamped tar.gz
- **Postcheck** — verifies gateway and heartbeat state after update
- **State persistence** — resumes from saved state after restart

## Environment variables
- `SAFE_UPDATE_KEEP_LAST_SUCCESS` — number of backups to keep (default: 3)
- `SAFE_UPDATE_MAX_AGE_DAYS` — max age for backups in days (default: 14)
- `SAFE_UPDATE_UPDATE_CMD` — update command to run (required for actual update)
- `SAFE_UPDATE_MIN_FREE_MB` — minimum free disk space in MB (default: 500)
- `SAFE_UPDATE_AUTO_ROLLBACK` — enable auto-rollback on postcheck failure (default: 1)
