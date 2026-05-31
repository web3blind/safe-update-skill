# safe-update

Stateful and low-risk Hermes Agent update workflow with precheck, backup, release-impact analysis, postcheck, optional rollback guidance, and cleanup.

This skill is designed for local/private operator use. It does not contain secrets and should not store `.env`, auth files, tokens, or private keys inside the repo.

## Features

- disk space check before backup/update;
- timestamped backup under `~/.hermes/state/safe-update/backups`;
- release-impact summary from the Hermes Agent GitHub releases feed;
- optional update command through `SAFE_UPDATE_UPDATE_CMD`;
- postcheck for `hermes --version`, `hermes status`, gateway status, and scheduler state;
- state persistence for resume/cleanup flows.

## Requirements

- Python 3.10+
- Hermes Agent installed and available as `hermes`

## Usage

```bash
# Dry-run / safety preflight
python3 ~/.hermes/skills/devops/safe-update/scripts/safe_update.py dry-run

# Full update with safety checks
SAFE_UPDATE_UPDATE_CMD="hermes update" \
  python3 ~/.hermes/skills/devops/safe-update/scripts/safe_update.py run

# Resume after interruption
python3 ~/.hermes/skills/devops/safe-update/scripts/safe_update.py resume

# Cleanup old backups
python3 ~/.hermes/skills/devops/safe-update/scripts/safe_update.py cleanup
```

## Environment Variables

- `SAFE_UPDATE_KEEP_LAST_SUCCESS` — number of backups to keep, default `3`.
- `SAFE_UPDATE_MAX_AGE_DAYS` — max age for backups, default `14`.
- `SAFE_UPDATE_UPDATE_CMD` — explicit update command. Required for actual update.
- `SAFE_UPDATE_MIN_FREE_MB` — minimum free disk space in MB, default `500`.
- `SAFE_UPDATE_AUTO_ROLLBACK` — keep for compatibility; manual review is recommended before rollback.
- `SAFE_UPDATE_GATEWAY_JOURNAL_LINES` — gateway log lines to inspect, default `200`.

## Safety model

Run `dry-run` first. Do not set `SAFE_UPDATE_UPDATE_CMD` to a destructive or unrelated command. Keep secrets outside the skill repo.

## License

MIT
