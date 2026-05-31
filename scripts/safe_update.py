#!/usr/bin/env python3
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen
from xml.etree import ElementTree as ET

ROOT = Path.home() / '.hermes'
STATE_DIR = ROOT / 'state' / 'safe-update'
BACKUP_DIR = STATE_DIR / 'backups'
STATE_FILE = STATE_DIR / 'state.json'
REPORT_FILE = STATE_DIR / 'last-report.md'
CFG_PATH = Path.home() / '.hermes' / 'config.yaml'
ENV_PATH = ROOT / '.env'
GLOBAL_HERMES_DIR = Path.home() / '.hermes' / 'hermes-agent'
GLOBAL_HERMES_ENTRYPOINT = GLOBAL_HERMES_DIR / 'run_agent.py'
GATEWAY_SERVICE_PATH = Path.home() / '.config' / 'systemd' / 'user' / 'hermes-gateway.service'
SAFE_UPDATE_CACHE_DIR = Path.home() / '.hermes' / 'tmp' / 'safe-update-cache'
GATEWAY_JOURNAL_RECENT_LINES = int(os.getenv('SAFE_UPDATE_GATEWAY_JOURNAL_LINES', '200'))


def load_env_file(path: Path):
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding='utf-8').splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass


load_env_file(ENV_PATH)

KEEP_LAST_SUCCESS = int(os.getenv('SAFE_UPDATE_KEEP_LAST_SUCCESS', '3'))
MAX_AGE_DAYS = int(os.getenv('SAFE_UPDATE_MAX_AGE_DAYS', '14'))
UPDATE_CMD = os.getenv('SAFE_UPDATE_UPDATE_CMD', '').strip()  # optional
MIN_FREE_SPACE_MB = int(os.getenv('SAFE_UPDATE_MIN_FREE_MB', '500'))  # minimum free space before backup
ENABLE_AUTO_ROLLBACK = os.getenv('SAFE_UPDATE_AUTO_ROLLBACK', '1') == '1'
SUCCESS_COOLDOWN_SECONDS = int(os.getenv('SAFE_UPDATE_SUCCESS_COOLDOWN_SECONDS', '900'))
# Current Hermes CLI uses `hermes update` (without legacy `run`).
# We also pin npm's cache to a dedicated temp dir because root's default cache
# can be left in a broken state after interrupted global installs, which causes
# `hermes update` -> `npm i -g ...` to fail with ENOENT inside /root/.npm/_cacache.
DEFAULT_UPDATE_CMD = 'hermes update'


def is_system_install(global_dir: Path = GLOBAL_HERMES_DIR, global_entrypoint: Path = GLOBAL_HERMES_ENTRYPOINT):
    return global_dir.exists() or global_entrypoint.exists()


def command_has_privilege_escalation(cmd: str) -> bool:
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        return True
    return bool(re.search(r'(^|[\s;&|])((/usr/bin|/bin)/)?(sudo|doas|su)(\s|$)', cmd))


def validate_update_command_for_install(cmd: str, system_install: bool | None = None) -> dict:
    system_install = is_system_install() if system_install is None else system_install
    if not system_install:
        return {'ok': True, 'reason': ''}
    if cmd.strip() == DEFAULT_UPDATE_CMD:
        return {'ok': True, 'reason': ''}
    if command_has_privilege_escalation(cmd):
        return {'ok': True, 'reason': ''}
    return {
        'ok': False,
        'reason': (
            'Unsafe custom SAFE_UPDATE_UPDATE_CMD for Hermes source install: '
            'effective update command does not use sudo/elevated execution, '
            'for this Hermes installation under ~/.hermes/hermes-agent. '
            'Aborting before apply to avoid EACCES/partial Hermes update.'
        ),
    }


def run(cmd: str):
    use_shell = bool(re.search(r'[|&;<>$`\\()]', cmd))
    args = cmd if use_shell else shlex.split(cmd)
    p = subprocess.run(args, shell=use_shell, capture_output=True, text=True)
    return p.returncode, (p.stdout or '').strip(), (p.stderr or '').strip()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(ts: str):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def ensure_dirs():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def load_state():
    ensure_dirs()
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {
        'createdAt': now_iso(),
        'updatedAt': now_iso(),
        'step': 'init',
        'stepsDone': [],
        'backupPath': '',
        'backupSnapshotDir': '',
        'updateExecuted': False,
        'updateSkipped': False,
        'notes': [],
    }


def save_state(st):
    st['updatedAt'] = now_iso()
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding='utf-8')


def add_note(st, text):
    st['notes'].append(f"[{now_iso()}] {text}")


def get_npm_cache_path(run_cmd=run):
    c, o, e = run_cmd('npm config get cache')
    path = (o or e).strip()
    return {'ok': c == 0 and bool(path), 'path': path, 'out': path or (e or o).strip()}


def check_npm_cache_health(run_cmd=run):
    cache_info = get_npm_cache_path(run_cmd)
    if not cache_info['ok']:
        return {'ok': False, 'path': cache_info.get('path', ''), 'out': cache_info.get('out', 'npm cache path unavailable')}

    cache_path = cache_info['path']
    path_obj = Path(cache_path)
    if not path_obj.exists():
        return {'ok': False, 'path': cache_path, 'out': 'npm cache path does not exist'}
    if not path_obj.is_dir():
        return {'ok': False, 'path': cache_path, 'out': 'npm cache path is not a directory'}

    c, o, e = run_cmd(f'npm cache verify --cache {cache_path}')
    return {'ok': c == 0, 'path': cache_path, 'out': (o or e)[:2000]}


def check_entrypoint_present(entrypoint: Path = GLOBAL_HERMES_ENTRYPOINT):
    exists = entrypoint.exists() and entrypoint.is_file()
    return {'ok': exists, 'path': str(entrypoint), 'out': str(entrypoint) if exists else f'missing: {entrypoint}'}


def summarize_install_health(checks: dict):
    required = ['hermes_version', 'entrypoint_present']
    failed = [name for name in required if not checks.get(name, {}).get('ok', False)]
    return {'ok': not failed, 'failed_checks': failed}


def summarize_postcheck(checks: dict):
    failed = [name for name, value in checks.items() if not value.get('ok', False)]
    return {'ok': not failed, 'failed_checks': failed}


def iso_to_journalctl_since(ts: str | None):
    parsed = parse_iso(ts or '') if ts else None
    if not parsed:
        return '10 minutes ago'
    return parsed.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def extract_ready_plugin_list(log_text: str):
    plugin_lists = []
    for line in log_text.splitlines():
        m = re.search(r'ready \((\d+) plugins?: ([^;]+?)(?:;|\))', line)
        if not m:
            continue
        plugins = [p.strip() for p in m.group(2).split(',') if p.strip()]
        plugin_lists.append(plugins)
    return plugin_lists


def analyze_gateway_journal(log_text: str):
    ready_plugin_lists = extract_ready_plugin_list(log_text)
    ready_with_telegram = any('telegram' in plugins for plugins in ready_plugin_lists)
    error_lines = []
    cache_permission_lines = []
    generic_permission_lines = []
    for line in log_text.splitlines():
        lower = line.lower()
        has_permission_issue = 'eacces' in lower or 'permission denied' in lower
        has_cache = 'cache' in lower
        has_telegram_failure = 'telegram failed' in lower or ('failed to load plugin' in lower and 'plugin=telegram' in lower)
        if has_telegram_failure or (has_permission_issue and has_cache):
            error_lines.append(line)
        if has_permission_issue and has_cache:
            cache_permission_lines.append(line)
        elif has_permission_issue:
            generic_permission_lines.append(line)

    return {
        'ready_plugin_lists': ready_plugin_lists,
        'ready_with_telegram': ready_with_telegram,
        'error_lines': error_lines,
        'cache_permission_lines': cache_permission_lines,
        'generic_permission_lines': generic_permission_lines,
    }


def should_attempt_cache_autofix(analysis: dict):
    if analysis.get('ready_with_telegram'):
        return False
    return bool(analysis.get('cache_permission_lines'))


def upsert_systemd_service_env(service_text: str, key: str, value: str):
    lines = service_text.splitlines()
    service_start = None
    service_end = len(lines)
    for idx, line in enumerate(lines):
        if line.strip() == '[Service]':
            service_start = idx
            continue
        if service_start is not None and idx > service_start and re.match(r'^\[.+\]$', line.strip()):
            service_end = idx
            break

    if service_start is None:
        raise RuntimeError('systemd unit is missing [Service] section')

    env_prefix = f'Environment={key}='
    quoted_value = shlex.quote(value)
    env_line = f'Environment={key}={quoted_value}'

    for idx in range(service_start + 1, service_end):
        if lines[idx].startswith(env_prefix):
            changed = lines[idx] != env_line
            lines[idx] = env_line
            return '\n'.join(lines) + ('\n' if service_text.endswith('\n') else ''), changed

    insert_at = service_end
    lines.insert(insert_at, env_line)
    return '\n'.join(lines) + ('\n' if service_text.endswith('\n') else ''), True


def ensure_gateway_cache_cache_fix(service_path: Path = GATEWAY_SERVICE_PATH, cache_dir: Path = SAFE_UPDATE_CACHE_DIR):
    if not service_path.exists():
        return {'ok': False, 'changed': False, 'reason': f'missing service file: {service_path}'}

    cache_dir.mkdir(parents=True, exist_ok=True)
    original = service_path.read_text(encoding='utf-8')
    updated, changed = upsert_systemd_service_env(original, 'CACHE_FS_CACHE', str(cache_dir))
    if changed:
        service_path.write_text(updated, encoding='utf-8')
    return {'ok': True, 'changed': changed, 'cache_dir': str(cache_dir), 'service_path': str(service_path)}


def restart_gateway(st, reason='post-update restart'):
    st['gatewayRestartAt'] = now_iso()
    c, o, e = run('systemctl --user daemon-reload && hermes gateway restart')
    out = (o or e)[:3000]
    add_note(st, f'Gateway restart ({reason}) ok={c == 0}')
    return {'ok': c == 0, 'out': out, 'at': st['gatewayRestartAt'], 'reason': reason}


def collect_gateway_journal(since_ts: str | None, limit: int = GATEWAY_JOURNAL_RECENT_LINES):
    since = iso_to_journalctl_since(since_ts)
    c, o, e = run(f'journalctl --user -u hermes-gateway --since {shlex.quote(since)} -n {int(limit)} --no-pager')
    out = (o or e)[:12000]
    analysis = analyze_gateway_journal(out if c == 0 else '')
    return {
        'ok': c == 0,
        'out': out,
        'analysis': analysis,
        'since': since,
    }


def build_gateway_plugin_checks(journal_result: dict):
    analysis = journal_result.get('analysis', {})
    out = journal_result.get('out', '')
    return {
        'gateway_journal_available': {
            'ok': journal_result.get('ok', False),
            'out': out[:2000] if out else f"journal since {journal_result.get('since', 'unknown')} unavailable",
        },
        'gateway_ready_with_telegram': {
            'ok': analysis.get('ready_with_telegram', False),
            'out': out[:2000] if out else 'telegram readiness not observed',
        },
        'gateway_no_cache_permission_errors': {
            'ok': not analysis.get('cache_permission_lines'),
            'out': '\n'.join(analysis.get('cache_permission_lines', [])[:10]) or 'no cache permission errors found',
        },
        'gateway_no_telegram_plugin_failures': {
            'ok': not any('telegram' in line.lower() for line in analysis.get('error_lines', [])),
            'out': '\n'.join([line for line in analysis.get('error_lines', []) if 'telegram' in line.lower()][:10]) or 'no telegram plugin failures found',
        },
    }


def should_skip_due_to_recent_success(st, now_ts=None, cooldown_seconds=SUCCESS_COOLDOWN_SECONDS):
    if cooldown_seconds <= 0:
        return {'skip': False, 'remaining_seconds': 0}

    last_success = parse_iso(st.get('lastSuccessfulUpdateAt', ''))
    current = parse_iso(now_ts) if now_ts else datetime.now(timezone.utc)
    if not last_success or not current:
        return {'skip': False, 'remaining_seconds': 0}

    elapsed = (current - last_success).total_seconds()
    if elapsed < cooldown_seconds:
        return {'skip': True, 'remaining_seconds': int(cooldown_seconds - elapsed)}
    return {'skip': False, 'remaining_seconds': 0}


def check_disk_space(path: Path, required_mb: int = MIN_FREE_SPACE_MB) -> dict:
    """Check available disk space. Returns dict with 'ok' and 'available_mb'."""
    try:
        stat = shutil.disk_usage(path)
        available_mb = stat.free / (1024 * 1024)
        return {'ok': available_mb >= required_mb, 'available_mb': int(available_mb), 'required_mb': required_mb}
    except Exception as e:
        return {'ok': False, 'error': str(e), 'available_mb': 0, 'required_mb': required_mb}


def precheck(st):
    checks = {}
    for name, cmd in [
        ('hermes_status', 'hermes status'),
        ('gateway_status', 'hermes gateway status'),
    ]:
        c, o, e = run(cmd)
        checks[name] = {'ok': c == 0, 'out': (o or e)[:2000]}

    # check disk space before backup
    disk_check = check_disk_space(BACKUP_DIR)
    checks['disk_space'] = {'ok': disk_check['ok'], 'out': f"Available: {disk_check.get('available_mb', 0)}MB, required: {disk_check.get('required_mb', MIN_FREE_SPACE_MB)}MB"}
    if not disk_check['ok']:
        add_note(st, f"WARNING: Low disk space ({disk_check.get('available_mb', 0)}MB)")

    # optional config validate
    c, o, e = run('hermes config validate --json')
    out = (o or e)
    if c != 0 and ('unknown' in out.lower() or 'not found' in out.lower() or 'invalid choice' in out.lower()):
        checks['config_validate'] = {'ok': True, 'out': 'skipped: command not available in this Hermes build'}
    else:
        checks['config_validate'] = {'ok': c == 0, 'out': out[:2000]}

    checks['npm_cache_health'] = check_npm_cache_health()
    checks['hermes_version'] = {'ok': False, 'out': ''}
    c, o, e = run('hermes --version')
    checks['hermes_version'] = {'ok': c == 0, 'out': (o or e)[:2000]}
    checks['entrypoint_present'] = check_entrypoint_present()
    checks['install_health'] = summarize_install_health(checks)
    if not checks['npm_cache_health']['ok']:
        add_note(st, 'WARNING: npm cache health preflight failed')
    if not checks['install_health']['ok']:
        add_note(st, f"WARNING: Existing install looks unsafe/partial: {', '.join(checks['install_health']['failed_checks'])}")

    st['precheck'] = checks
    st['stepsDone'].append('precheck')
    add_note(st, 'Precheck completed')


def backup(st):
    ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    bdir = BACKUP_DIR / ts
    bdir.mkdir(parents=True, exist_ok=True)

    if CFG_PATH.exists():
        shutil.copy2(CFG_PATH, bdir / 'hermes.json')

    # key context files
    key_files = [
        ROOT / 'MEMORY.md', ROOT / 'AGENTS.md', ROOT / 'SOUL.md', ROOT / 'USER.md',
        ROOT / 'chats.md', ROOT / 'CHATS.md', ROOT / 'memory' / 'telegram-topics.json',
        GATEWAY_SERVICE_PATH,
    ]
    for p in key_files:
        if p.exists():
            rel = p.relative_to(ROOT) if str(p).startswith(str(ROOT)) else Path(p.name)
            out = bdir / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out)

    # snapshot skill configs/state
    state_digest = Path('/home/assistent/.hermes/state/digest/configs')
    if state_digest.exists():
        out = bdir / 'state' / 'digest' / 'configs'
        out.mkdir(parents=True, exist_ok=True)
        for f in state_digest.glob('*.json'):
            shutil.copy2(f, out / f.name)

    # compressed archive
    tar_path = BACKUP_DIR / f'{ts}.tar.gz'
    with tarfile.open(tar_path, 'w:gz') as tar:
        tar.add(bdir, arcname=ts)

    st['backupPath'] = str(tar_path)
    st['backupSnapshotDir'] = str(bdir)
    st['stepsDone'].append('backup')
    add_note(st, f'Backup completed: {tar_path}')


def fetch_release_summary(limit=2):
    url = 'https://github.com/NousResearch/hermes-agent/releases.atom'
    with urlopen(url, timeout=20) as r:
        data = r.read()
    root = ET.fromstring(data)
    ns = {'a': 'http://www.w3.org/2005/Atom'}
    items = []
    for e in root.findall('a:entry', ns)[:limit]:
        title = (e.findtext('a:title', default='', namespaces=ns) or '').strip()
        link_el = e.find('a:link', ns)
        link = link_el.attrib.get('href', '') if link_el is not None else ''
        content = (e.findtext('a:content', default='', namespaces=ns) or '')
        # strip HTML tags crudely
        txt = re.sub('<[^>]+>', ' ', content)
        txt = re.sub(r'\s+', ' ', txt).strip()
        items.append({'title': title, 'link': link, 'summary': txt[:1200]})
    return items


def analyze_release_impact(st):
    items = fetch_release_summary(limit=3)
    risk_keywords = ['telegram', 'topic', 'session', 'cron', 'heartbeat', 'stream', 'config', 'plugin', 'routing']
    impacts = []
    for it in items:
        text = (it['title'] + ' ' + it['summary']).lower()
        hits = [k for k in risk_keywords if k in text]
        impacts.append({'release': it['title'], 'link': it['link'], 'riskHits': hits})
    st['releaseImpact'] = impacts
    st['stepsDone'].append('release_impact')
    add_note(st, 'Release impact analysis completed')


def execute_update_step(st):
    cmd = UPDATE_CMD if UPDATE_CMD else DEFAULT_UPDATE_CMD
    add_note(st, f'Using update command: {cmd}')
    st['updateStartedAt'] = now_iso()

    command_check = validate_update_command_for_install(cmd)
    st['updateCommandSafe'] = command_check['ok']
    if not command_check['ok']:
        st['updateExecuted'] = False
        st['updateSkipped'] = False
        st['updateCommandOk'] = False
        st['updateOutput'] = command_check['reason']
        add_note(st, f"ERROR: {command_check['reason']}")
        raise RuntimeError(command_check['reason'])

    cooldown = should_skip_due_to_recent_success(st)
    if cooldown['skip']:
        st['updateExecuted'] = False
        st['updateSkipped'] = True
        st['updateOutput'] = f"Skipped: previous successful update is still in cooldown ({cooldown['remaining_seconds']}s remaining)"
        add_note(st, st['updateOutput'])
        return

    c, o, e = run(cmd)
    st['updateExecuted'] = (c == 0)
    st['updateCommandOk'] = (c == 0)
    st['updateOutput'] = (o or e)[:3000]
    add_note(st, f"Update command executed, ok={st['updateCommandOk']}")


def postcheck(st):
    checks = {}
    for name, cmd in [
        ('hermes_status_after', 'hermes status'),
        ('gateway_status_after', 'hermes gateway status'),
    ]:
        c, o, e = run(cmd)
        checks[name] = {'ok': c == 0, 'out': (o or e)[:2000]}

    # lightweight cron smoke
    c, o, e = run('python3 - <<\'PY\'\nimport json, pathlib\np=pathlib.Path("/home/assistent/.hermes/workspace/memory/heartbeat-state.json")\nprint("ok" if p.exists() else "missing")\nPY')
    checks['heartbeat_state_present'] = {'ok': c == 0 and 'ok' in (o or ''), 'out': (o or e)[:2000]}
    c, o, e = run('hermes --version')
    checks['hermes_version_after'] = {'ok': c == 0, 'out': (o or e)[:2000]}
    checks['entrypoint_present_after'] = check_entrypoint_present()

    journal_result = collect_gateway_journal(st.get('gatewayRestartAt') or st.get('updateStartedAt'))
    checks.update(build_gateway_plugin_checks(journal_result))

    analysis = journal_result.get('analysis', {})
    autofix_result = {'ok': True, 'skipped': True, 'out': 'not needed'}
    if should_attempt_cache_autofix(analysis):
        fix = ensure_gateway_cache_cache_fix()
        if not fix.get('ok'):
            autofix_result = {'ok': False, 'skipped': False, 'out': fix.get('reason', 'cache auto-fix failed')}
            add_note(st, f"ERROR: cache auto-fix failed: {autofix_result['out']}")
        else:
            add_note(st, f"Applied cache cache fix (changed={fix.get('changed', False)} path={fix.get('cache_dir')})")
            restart = restart_gateway(st, reason='cache cache auto-fix')
            if not restart.get('ok'):
                autofix_result = {'ok': False, 'skipped': False, 'out': restart.get('out', 'gateway restart failed after cache auto-fix')}
                add_note(st, 'ERROR: gateway restart failed after cache auto-fix')
            else:
                retry_journal = collect_gateway_journal(restart.get('at'))
                checks.update({
                    f'{key}_after_fix': value for key, value in build_gateway_plugin_checks(retry_journal).items()
                })
                retry_analysis = retry_journal.get('analysis', {})
                retry_ok = retry_journal.get('ok', False) and retry_analysis.get('ready_with_telegram', False) and not retry_analysis.get('cache_permission_lines')
                autofix_result = {
                    'ok': retry_ok,
                    'skipped': False,
                    'out': retry_journal.get('out', '')[:2000] or 'retry journal unavailable',
                }
                add_note(st, f'Cache auto-fix verification ok={retry_ok}')
    checks['gateway_cache_autofix'] = autofix_result

    checks['postcheck_summary'] = summarize_postcheck({
        k: v for k, v in checks.items() if k != 'postcheck_summary'
    })

    st['postcheck'] = checks
    st['stepsDone'].append('postcheck')
    add_note(st, 'Postcheck completed')
    return checks


def rollback_from_backup(backup_path: str) -> bool:
    """Restore from backup archive. Returns True if successful."""
    if not backup_path or not Path(backup_path).exists():
        return False
    try:
        # Extract to temp location first
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with tarfile.open(backup_path, 'r:gz') as tar:
                tar.extractall(tmpdir)
            # Find the extracted folder
            extracted = list(Path(tmpdir).iterdir())
            if not extracted:
                return False
            # Restore key files
            root_dir = extracted[0]
            for f in ['MEMORY.md', 'AGENTS.md', 'SOUL.md', 'USER.md']:
                src = root_dir / f
                if src.exists():
                    shutil.copy2(src, ROOT / f)
            # Restore memory files
            src_memory = root_dir / 'memory'
            if src_memory.exists():
                dst_memory = ROOT / 'memory'
                dst_memory.mkdir(parents=True, exist_ok=True)
                for f in src_memory.glob('*'):
                    if f.is_file():
                        shutil.copy2(f, dst_memory / f.name)
            # Restore config
            src_cfg = root_dir / 'hermes.json'
            if src_cfg.exists():
                shutil.copy2(src_cfg, CFG_PATH)
            src_service = root_dir / GATEWAY_SERVICE_PATH.name
            if src_service.exists():
                GATEWAY_SERVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_service, GATEWAY_SERVICE_PATH)
        return True
    except Exception as e:
        print(f"Rollback failed: {e}")
        return False


def cleanup_backups(st):
    ensure_dirs()
    archives = sorted(BACKUP_DIR.glob('*.tar.gz'), key=lambda p: p.stat().st_mtime, reverse=True)

    # keep last N
    for idx, p in enumerate(archives):
        if idx >= KEEP_LAST_SUCCESS:
            try:
                p.unlink()
            except Exception:
                pass

    # age-based
    cutoff = time.time() - (MAX_AGE_DAYS * 86400)
    for p in BACKUP_DIR.glob('*.tar.gz'):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except Exception:
            pass

    st['stepsDone'].append('cleanup')
    add_note(st, f'Cleanup completed (keep={KEEP_LAST_SUCCESS}, age={MAX_AGE_DAYS}d)')


def render_report(st):
    ensure_dirs()
    lines = []
    lines.append('# Safe Update Report')
    lines.append(f"- Updated: {st.get('updatedAt')}")
    lines.append(f"- Backup: {st.get('backupPath') or 'none'}")
    lines.append(f"- Update executed: {st.get('updateExecuted', False)}")
    lines.append(f"- Update skipped: {st.get('updateSkipped', False)}")
    gateway_restart = st.get('gatewayRestart', {}) if isinstance(st.get('gatewayRestart'), dict) else {}
    lines.append(f"- Gateway restart: {'ok' if gateway_restart.get('ok') else 'not-run'}")
    postcheck = st.get('postcheck', {}) if isinstance(st.get('postcheck'), dict) else {}
    telegram_ready = postcheck.get('gateway_ready_with_telegram', {}).get('ok')
    if telegram_ready is None and 'gateway_ready_with_telegram_after_fix' in postcheck:
        telegram_ready = postcheck.get('gateway_ready_with_telegram_after_fix', {}).get('ok')
    lines.append(f"- Telegram ready: {telegram_ready if telegram_ready is not None else 'unknown'}")
    autofix = postcheck.get('gateway_cache_autofix', {}) if isinstance(postcheck.get('gateway_cache_autofix'), dict) else {}
    if autofix:
        if autofix.get('skipped'):
            autofix_status = 'skipped'
        else:
            autofix_status = 'ok' if autofix.get('ok') else 'failed'
    else:
        autofix_status = 'unknown'
    lines.append(f"- Cache auto-fix: {autofix_status}")
    lines.append('')

    if st.get('releaseImpact'):
        lines.append('## Release impact')
        for r in st['releaseImpact']:
            lines.append(f"- {r['release']}: {', '.join(r['riskHits']) if r['riskHits'] else 'no major hits'}")
            if r.get('link'):
                lines.append(f"  - {r['link']}")
        lines.append('')

    if st.get('precheck'):
        lines.append('## Precheck')
        for k, v in st['precheck'].items():
            lines.append(f"- {k}: {'ok' if v.get('ok') else 'fail'}")
        lines.append('')

    if st.get('postcheck'):
        lines.append('## Postcheck')
        for k, v in st['postcheck'].items():
            if k == 'postcheck_summary':
                lines.append(f"- {k}: {'ok' if v.get('ok') else 'fail'} ({', '.join(v.get('failed_checks', [])) or 'no failed checks'})")
            else:
                lines.append(f"- {k}: {'ok' if v.get('ok') else 'fail'}")
        lines.append('')

    if gateway_restart:
        lines.append('## Gateway activation')
        lines.append(f"- restart: {'ok' if gateway_restart.get('ok') else 'fail'}")
        if gateway_restart.get('reason'):
            lines.append(f"- reason: {gateway_restart.get('reason')}")
        if gateway_restart.get('at'):
            lines.append(f"- at: {gateway_restart.get('at')}")
        lines.append('')

    if postcheck:
        lines.append('## Telegram plugin readiness')
        readiness_keys = [
            'gateway_ready_with_telegram',
            'gateway_ready_with_telegram_after_fix',
            'gateway_no_cache_permission_errors',
            'gateway_no_cache_permission_errors_after_fix',
            'gateway_no_telegram_plugin_failures',
            'gateway_no_telegram_plugin_failures_after_fix',
            'gateway_cache_autofix',
        ]
        for key in readiness_keys:
            if key in postcheck:
                value = postcheck[key]
                lines.append(f"- {key}: {'ok' if value.get('ok') else 'fail'}")
        lines.append('')

    if st.get('rollbackPerformed'):
        lines.append('## Rollback')
        lines.append('- Status: performed (postcheck failed)')
        lines.append('')
    elif st.get('rollbackFailed'):
        lines.append('## Rollback')
        lines.append('- Status: FAILED')
        lines.append('')

    lines.append('## Notes')
    for n in st.get('notes', []):
        lines.append(f"- {n}")

    txt = '\n'.join(lines)
    REPORT_FILE.write_text(txt, encoding='utf-8')
    print(txt)


def do_dry_run(st):
    if 'precheck' not in st['stepsDone']:
        precheck(st)
    # Check disk space before proceeding
    disk_check = check_disk_space(BACKUP_DIR)
    if not disk_check['ok']:
        add_note(st, f"WARNING: Not enough disk space ({disk_check.get('available_mb', 0)}MB)")
    if 'backup' not in st['stepsDone']:
        backup(st)
    if 'release_impact' not in st['stepsDone']:
        analyze_release_impact(st)
    cleanup_backups(st)


def do_run(st):
    if 'precheck' not in st['stepsDone']:
        precheck(st)
    precheck_result = st.get('precheck', {})
    if not precheck_result.get('npm_cache_health', {}).get('ok', False):
        add_note(st, 'ERROR: npm cache preflight failed. Aborting update.')
        st['stepsDone'].append('abort')
        return
    if not precheck_result.get('install_health', {}).get('ok', False):
        add_note(st, 'ERROR: Existing Hermes install looks partial/unsafe. Aborting update.')
        st['stepsDone'].append('abort')
        return
    # Check disk space before proceeding
    disk_check = check_disk_space(BACKUP_DIR)
    if not disk_check['ok']:
        add_note(st, f"ERROR: Not enough disk space. Aborting update.")
        st['stepsDone'].append('abort')
        return
    if 'backup' not in st['stepsDone']:
        backup(st)
    if 'release_impact' not in st['stepsDone']:
        analyze_release_impact(st)
    if 'update' not in st['stepsDone']:
        try:
            execute_update_step(st)
        except RuntimeError:
            st['stepsDone'].append('abort')
            return
        st['stepsDone'].append('update')
    if st.get('updateCommandOk') and not st.get('updateSkipped') and 'gateway_restart' not in st['stepsDone']:
        restart = restart_gateway(st, reason='post-update activation')
        st['gatewayRestart'] = restart
        st['stepsDone'].append('gateway_restart')
        if not restart.get('ok'):
            add_note(st, 'ERROR: gateway restart failed after update. Aborting before postcheck.')
            st['stepsDone'].append('abort')
            return
    if 'postcheck' not in st['stepsDone']:
        postcheck_results = postcheck(st)
        summary = postcheck_results.get('postcheck_summary', {'ok': False, 'failed_checks': ['missing_summary']})
        st['updateVerified'] = summary.get('ok', False)
        if st['updateVerified']:
            st['lastSuccessfulUpdateAt'] = now_iso()
            st['updateExecuted'] = True
        else:
            st['updateExecuted'] = False
        # Auto-rollback if postcheck failed
        if ENABLE_AUTO_ROLLBACK and st.get('updateCommandOk') and not st.get('updateSkipped'):
            postcheck_ok = summary.get('ok', False)
            if not postcheck_ok:
                add_note(st, "WARNING: Postcheck failed, initiating rollback...")
                backup_path = st.get('backupPath', '')
                if rollback_from_backup(backup_path):
                    add_note(st, "Rollback completed successfully")
                    st['rollbackPerformed'] = True
                else:
                    add_note(st, "ERROR: Rollback failed!")
                    st['rollbackFailed'] = True
    if 'cleanup' not in st['stepsDone']:
        cleanup_backups(st)


def main():
    ensure_dirs()
    st = load_state()
    cmd = (os.sys.argv[1] if len(os.sys.argv) > 1 else 'dry-run').strip().lower()

    if cmd == 'dry-run':
        do_dry_run(st)
    elif cmd == 'run':
        do_run(st)
    elif cmd == 'resume':
        do_run(st)
    elif cmd == 'cleanup':
        cleanup_backups(st)
    else:
        print('Usage: safe_update.py [dry-run|run|resume|cleanup]')
        raise SystemExit(2)

    save_state(st)
    render_report(st)


if __name__ == '__main__':
    main()
